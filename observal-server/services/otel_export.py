# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Export stored sessions as OpenTelemetry traces (OTLP/JSON).

Sessions are stored as raw harness transcript lines in ``session_events``.
This module derives an OTLP ``ExportTraceServiceRequest`` from them at read
time, so storage stays lossless and the span shape can improve without a
migration.

Input is the output of ``services.session_parsers.parse_raw_events`` (the
normalized event vocabulary shared by every harness parser) plus the stored
ClickHouse rows, whose ``event_type`` column is normalized at ingest.  No
harness-specific logic lives here.

Span tree per session::

    invoke_agent <agent>            (the session)
      turn N                        (one per user prompt)
        chat <model>                (one per model response)
        execute_tool <tool>         (one per tool call)
      invoke_agent subagent         (subagent sessions, same trace)

Attributes follow the OpenTelemetry GenAI semantic conventions
(``gen_ai.*``) plus ``input.value`` / ``output.value``, which Langfuse,
LangSmith and Phoenix all read.  Transcripts carry one timestamp per record,
not start/end pairs, so span durations are derived from neighbouring records.

Trace and span IDs are hashes of the session ID and a stable per-span key, so
re-exporting a session produces the same IDs.  Backends that upsert by span ID
update in place; append-only stores such as Jaeger keep both copies.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

SCOPE_NAME = "observal.session_export"

MAX_CONTENT_CHARS = 32_000

SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_CODE_ERROR = 2

# Event names emitted by the session parsers.  ``user_prompt`` and friends are
# the raw ``event_type`` values that ``basic_event`` falls back to when a line
# could not be parsed.
_PROMPT_EVENTS = frozenset({"hook_userpromptsubmit", "user_prompt"})
_ASSISTANT_EVENTS = frozenset({"hook_assistant_response", "hook_assistant_thinking", "assistant_text", "thinking"})
_USAGE_EVENTS = frozenset({"hook_token_usage", "usage"})
_TOOL_CALL_EVENTS = frozenset({"hook_pretooluse", "hook_posttooluse", "tool_call"})
_TOOL_RESULT_EVENTS = frozenset({"hook_toolresult", "tool_result"})
_TEXT_EVENTS = frozenset({"hook_assistant_response", "assistant_text"})
_THINKING_EVENTS = frozenset({"hook_assistant_thinking", "thinking"})

_TOKEN_KEYS = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cache_read_tokens": "gen_ai.usage.cache_read.input_tokens",
    "cache_creation_tokens": "gen_ai.usage.cache_creation.input_tokens",
    "cache_write_tokens": "gen_ai.usage.cache_creation.input_tokens",
}

_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
_EPOCH_SENTINEL = "1970-01-01"


@dataclass
class SessionTrace:
    """One stored session: its ClickHouse rows and their parsed events."""

    session_id: str
    harness: str
    rows: list[dict]
    events: list[dict]
    spawned_by: str | None = None


@dataclass
class _Tool:
    key: str
    tool_id: str
    name: str
    start_ns: int
    input: str = ""
    output: str = ""
    result_ns: int | None = None
    error: bool = False


@dataclass
class _Generation:
    key: str
    start_ns: int
    end_ns: int
    texts: list[str] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    tool_calls: list[_Tool] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    model: str = ""
    finish_reasons: list[str] = field(default_factory=list)
    # Set once a result arrives for one of this response's tool calls: the
    # next response or tool call belongs to a new model call.
    answered: bool = False


@dataclass
class _Turn:
    key: str
    index: int
    start_ns: int
    prompt: str = ""
    children: list[_Generation | _Tool] = field(default_factory=list)
    span_events: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def trace_id_for(session_id: str) -> str:
    return hashlib.sha256(f"observal-trace:{session_id}".encode()).hexdigest()[:32]


def _span_id(session_id: str, key: str) -> str:
    return hashlib.sha256(f"observal-span:{session_id}:{key}".encode()).hexdigest()[:16]


def to_unix_nanos(value: object) -> int | None:
    """Parse a stored or transcript timestamp into Unix nanoseconds (UTC)."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number <= 0:
            return None
        # Millisecond epochs are 13 digits; second epochs are 10.
        return int(number * 1_000_000) if number > 1e11 else int(number * 1_000_000_000)
    text = str(value).strip()
    if not text or _EPOCH_SENTINEL in text:
        return None
    text = text.replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    text = _FRACTION_RE.sub(r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _to_int(value: object) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _clip(text: str) -> str:
    if len(text) <= MAX_CONTENT_CHARS:
        return text
    return text[:MAX_CONTENT_CHARS] + "...[truncated]"


def _any_value(value: object) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list | tuple):
        return {"arrayValue": {"values": [_any_value(item) for item in value]}}
    return {"stringValue": str(value)}


def _attributes(values: dict[str, object]) -> list[dict]:
    return [
        {"key": key, "value": _any_value(value)} for key, value in values.items() if value is not None and value != ""
    ]


def _json_or_text(value: str) -> object:
    try:
        return json.loads(value)
    except ValueError:
        return value


def _text(event: dict) -> str:
    attrs = event.get("attributes") or {}
    return str(attrs.get("tool_response") or attrs.get("tool_input") or event.get("body") or "")


# ---------------------------------------------------------------------------
# Session -> turns / generations / tools
# ---------------------------------------------------------------------------


class _SessionWalker:
    """Groups one session's parsed events into turns, model calls and tool calls."""

    def __init__(self, trace: SessionTrace):
        self.trace = trace
        self.turns: list[_Turn] = []
        self.session_events: list[dict] = []
        self.tools: list[_Tool] = []
        self._turn: _Turn | None = None
        self._generation: _Generation | None = None
        self._open_tools: dict[str, _Tool] = {}
        self._prev_ns: int | None = None
        self._gen_count = 0

    def walk(self) -> None:
        for index, event in enumerate(self.trace.events):
            ts = to_unix_nanos(event.get("timestamp")) or self._prev_ns
            if ts is None:
                continue
            self._handle(index, event, ts)
            self._prev_ns = ts
        self._generation = None

    def _handle(self, index: int, event: dict, ts: int) -> None:
        name = str(event.get("event_name") or "")
        attrs = event.get("attributes") or {}
        if name in _PROMPT_EVENTS:
            self._generation = None
            self._turn = _Turn(key=f"turn:{index}", index=len(self.turns) + 1, start_ns=ts, prompt=_text(event))
            self.turns.append(self._turn)
        elif name in _ASSISTANT_EVENTS:
            gen = self._current_generation(ts, new_after_tools=True, new_after_results=True)
            gen.end_ns = ts
            if name in _TEXT_EVENTS:
                gen.texts.append(_text(event))
            elif name in _THINKING_EVENTS:
                gen.thinking.append(_text(event))
            self._add_usage(gen, attrs)
        elif name in _USAGE_EVENTS:
            # Usage belongs to the response it was reported with, even when
            # that response ended in tool calls.
            gen = self._current_generation(ts, new_after_tools=False, new_after_results=False)
            gen.end_ns = max(gen.end_ns, ts)
            self._add_usage(gen, attrs)
        elif name in _TOOL_CALL_EVENTS or name in _TOOL_RESULT_EVENTS:
            self._handle_tool(index, name, attrs, ts)
        else:
            target = self._turn.span_events if self._turn else self.session_events
            target.append({"name": name or "event", "time": ts, "body": str(event.get("body") or "")})

    def _ensure_turn(self, ts: int) -> _Turn:
        if self._turn is None:
            self._turn = _Turn(key="turn:implicit", index=len(self.turns) + 1, start_ns=ts)
            self.turns.append(self._turn)
        return self._turn

    def _current_generation(self, ts: int, *, new_after_tools: bool, new_after_results: bool) -> _Generation:
        gen = self._generation
        if gen is None or (new_after_tools and gen.tool_calls) or (new_after_results and gen.answered):
            self._gen_count += 1
            start = self._prev_ns if self._prev_ns is not None else ts
            gen = _Generation(key=f"chat:{self._gen_count}", start_ns=min(start, ts), end_ns=ts)
            self._ensure_turn(ts).children.append(gen)
            self._generation = gen
        return gen

    @staticmethod
    def _add_usage(gen: _Generation, attrs: dict) -> None:
        for source in _TOKEN_KEYS:
            count = _to_int(attrs.get(source))
            if count:
                gen.tokens[source] = gen.tokens.get(source, 0) + count
        if attrs.get("model"):
            gen.model = str(attrs["model"])
        reason = attrs.get("stop_reason")
        if reason and reason not in gen.finish_reasons:
            gen.finish_reasons.append(str(reason))

    def _handle_tool(self, index: int, name: str, attrs: dict, ts: int) -> None:
        tool_id = str(attrs.get("tool_use_id") or attrs.get("tool_id") or "")
        tool_name = str(attrs.get("tool_name") or "")
        existing = self._open_tools.get(tool_id) if tool_id else None
        if existing is None and name in _TOOL_RESULT_EVENTS:
            existing = next(
                (tool for tool in reversed(self.tools) if tool.result_ns is None and tool.name == tool_name), None
            )
        if existing is not None:
            # A separate result record for a call we already saw.
            existing.result_ns = ts
            existing.output = str(attrs.get("tool_response") or existing.output)
            existing.error = existing.error or _is_error(attrs)
            if self._generation is not None and existing in self._generation.tool_calls:
                self._generation.answered = True
            return

        tool = _Tool(
            key=f"tool-id:{tool_id}" if tool_id else f"tool-event:{index}",
            tool_id=tool_id,
            name=tool_name or "tool",
            start_ns=ts,
            input=str(attrs.get("tool_input") or ""),
            output=str(attrs.get("tool_response") or ""),
            error=_is_error(attrs),
        )
        if name in _TOOL_RESULT_EVENTS:
            tool.result_ns = ts
        self.tools.append(tool)
        if tool_id:
            self._open_tools[tool_id] = tool
        if name not in _TOOL_RESULT_EVENTS:
            gen = self._current_generation(ts, new_after_tools=False, new_after_results=True)
            gen.end_ns = max(gen.end_ns, ts)
            gen.tool_calls.append(tool)
        self._ensure_turn(ts).children.append(tool)


def _is_error(attrs: dict) -> bool:
    return str(attrs.get("success", "")).lower() == "false" or str(attrs.get("is_error", "")).lower() == "true"


def _resolve_tool_ends(walker: _SessionWalker, rows: list[dict]) -> dict[str, int]:
    """Pick an end time for every tool span.

    Preference: a separate result event, then the stored ``tool_result`` row
    with the same tool ID, then the earliest unclaimed ``tool_result`` row at
    or after the call (harnesses write one result record per call, so
    parallel calls each claim their own).  Falls back to the call time (zero
    duration).
    """
    results: list[tuple[int, str]] = []
    for row in rows:
        if row.get("event_type") != "tool_result":
            continue
        ts = to_unix_nanos(row.get("timestamp"))
        if ts is not None:
            results.append((ts, str(row.get("tool_id") or "")))
    results.sort()
    result_times = [ts for ts, _tool_id in results]
    by_id: dict[str, int] = {}
    for pos, (_ts, tool_id) in enumerate(results):
        if tool_id:
            by_id.setdefault(tool_id, pos)
    claimed: set[int] = set()

    ends: dict[str, int | None] = {}
    for tool in walker.tools:
        end = tool.result_ns
        if end is None and tool.tool_id in by_id:
            claimed.add(by_id[tool.tool_id])
            end = result_times[by_id[tool.tool_id]]
        ends[tool.key] = end if end is None else max(end, tool.start_ns)
    for tool in sorted(walker.tools, key=lambda t: t.start_ns):
        if ends[tool.key] is not None:
            continue
        pos = bisect.bisect_left(result_times, tool.start_ns)
        while pos < len(results) and (pos in claimed or (results[pos][1] and results[pos][1] != tool.tool_id)):
            pos += 1
        if pos < len(results):
            claimed.add(pos)
            ends[tool.key] = result_times[pos]
        else:
            ends[tool.key] = tool.start_ns
    return {key: end for key, end in ends.items() if end is not None}


# ---------------------------------------------------------------------------
# Span assembly
# ---------------------------------------------------------------------------


class _SpanWriter:
    def __init__(self, trace_id: str, include_content: bool):
        self.trace_id = trace_id
        self.include_content = include_content
        self.spans: list[dict] = []

    def content(self, value: str) -> str | None:
        return _clip(value) if self.include_content and value else None

    def add(
        self,
        *,
        span_id: str,
        parent_id: str | None,
        name: str,
        kind: int,
        start_ns: int,
        end_ns: int,
        attributes: dict[str, object],
        events: list[dict] | None = None,
        error: bool = False,
    ) -> None:
        span: dict = {
            "traceId": self.trace_id,
            "spanId": span_id,
            "name": name,
            "kind": kind,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(max(end_ns, start_ns)),
            "attributes": _attributes(attributes),
        }
        if parent_id:
            span["parentSpanId"] = parent_id
        if events:
            span["events"] = [
                {
                    "timeUnixNano": str(item["time"]),
                    "name": item["name"],
                    "attributes": _attributes({"observal.event.body": self.content(item["body"])}),
                }
                for item in events
            ]
        if error:
            span["status"] = {"code": STATUS_CODE_ERROR}
        self.spans.append(span)


def _write_session(
    writer: _SpanWriter,
    trace: SessionTrace,
    *,
    parent_id: str | None,
    session_attributes: dict[str, object],
    root_name: str,
) -> None:
    walker = _SessionWalker(trace)
    walker.walk()
    tool_ends = _resolve_tool_ends(walker, trace.rows)
    sid = trace.session_id
    session_span_id = _span_id(sid, "session")

    first_prompt = next((turn.prompt for turn in walker.turns if turn.prompt), "")
    last_output = ""
    all_times: list[int] = [item["time"] for item in walker.session_events]

    for turn in walker.turns:
        turn_span_id = _span_id(sid, turn.key)
        turn_times = [turn.start_ns] + [item["time"] for item in turn.span_events]
        turn_output = ""
        earlier_tool_ends: list[int] = []
        for child in turn.children:
            if isinstance(child, _Generation):
                end = child.end_ns
                # A response that follows tool calls starts once their results
                # are back, not when the calls were made.
                start = max([child.start_ns] + [t for t in earlier_tool_ends if t <= end])
                text = "\n\n".join(t for t in child.texts if t)
                if text:
                    turn_output = text
                output = text or (
                    json.dumps(
                        [{"name": tool.name, "arguments": _json_or_text(tool.input)} for tool in child.tool_calls]
                    )
                    if child.tool_calls
                    else ""
                )
                input_tokens = child.tokens.get("input_tokens", 0)
                output_tokens = child.tokens.get("output_tokens", 0)
                attributes: dict[str, object] = {
                    "gen_ai.operation.name": "chat",
                    "langfuse.observation.type": "generation",
                    "langsmith.span.kind": "llm",
                    "gen_ai.request.model": child.model,
                    "gen_ai.response.model": child.model,
                    "gen_ai.response.finish_reasons": child.finish_reasons or None,
                    "output.value": writer.content(output),
                    "observal.reasoning": writer.content("\n\n".join(t for t in child.thinking if t)),
                }
                for source, key in _TOKEN_KEYS.items():
                    if child.tokens.get(source):
                        attributes[key] = child.tokens[source]
                if input_tokens or output_tokens:
                    attributes["gen_ai.usage.total_tokens"] = input_tokens + output_tokens
                writer.add(
                    span_id=_span_id(sid, child.key),
                    parent_id=turn_span_id,
                    name=f"chat {child.model}" if child.model else "chat",
                    kind=SPAN_KIND_CLIENT,
                    start_ns=start,
                    end_ns=end,
                    attributes=attributes,
                )
                turn_times += [start, end]
            else:
                end = tool_ends[child.key]
                earlier_tool_ends.append(end)
                writer.add(
                    span_id=_span_id(sid, child.key),
                    parent_id=turn_span_id,
                    name=f"execute_tool {child.name}",
                    kind=SPAN_KIND_INTERNAL,
                    start_ns=child.start_ns,
                    end_ns=end,
                    attributes={
                        "gen_ai.operation.name": "execute_tool",
                        "langfuse.observation.type": "tool",
                        "langsmith.span.kind": "tool",
                        "gen_ai.tool.name": child.name,
                        "gen_ai.tool.call.id": child.tool_id,
                        "input.value": writer.content(child.input),
                        "output.value": writer.content(child.output),
                    },
                    error=child.error,
                )
                turn_times += [child.start_ns, end]
        if turn_output:
            last_output = turn_output
        writer.add(
            span_id=turn_span_id,
            parent_id=session_span_id,
            name=f"turn {turn.index}",
            kind=SPAN_KIND_INTERNAL,
            start_ns=min(turn_times),
            end_ns=max(turn_times),
            attributes={
                "langsmith.span.kind": "chain",
                "observal.turn.index": turn.index,
                "input.value": writer.content(turn.prompt),
                "output.value": writer.content(turn_output),
            },
            events=turn.span_events,
        )
        all_times += turn_times

    if not all_times:
        all_times = [to_unix_nanos(row.get("timestamp")) or 0 for row in trace.rows] or [0]

    writer.add(
        span_id=session_span_id,
        parent_id=parent_id,
        name=root_name,
        kind=SPAN_KIND_INTERNAL,
        start_ns=min(all_times),
        end_ns=max(all_times),
        attributes={
            **session_attributes,
            "gen_ai.operation.name": "invoke_agent",
            "langfuse.observation.type": "agent",
            "langsmith.span.kind": "chain",
            "observal.session.id": sid,
            "observal.harness": trace.harness,
            "input.value": writer.content(first_prompt),
            "output.value": writer.content(last_output),
        },
        events=walker.session_events,
    )


def build_otlp_request(
    session: SessionTrace,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    agent_version: str | None = None,
    subagents: list[SessionTrace] | None = None,
    include_content: bool = False,
) -> dict:
    """Build an OTLP/JSON ``ExportTraceServiceRequest`` for one session.

    Prompt, response and tool payload text is only included when
    ``include_content`` is set; structure, timing, models, token usage and
    tool names are always exported.
    """
    writer = _SpanWriter(trace_id_for(session.session_id), include_content)
    _write_session(
        writer,
        session,
        parent_id=None,
        root_name=f"invoke_agent {agent_name or session.harness}",
        session_attributes={
            "session.id": session.session_id,
            "gen_ai.conversation.id": session.session_id,
            "langsmith.metadata.session_id": session.session_id,
            "user.id": user_id,
            "gen_ai.agent.id": agent_id,
            "gen_ai.agent.name": agent_name,
            "observal.agent.version": agent_version,
        },
    )
    session_span_id = _span_id(session.session_id, "session")
    for sub in subagents or []:
        _write_session(
            writer,
            sub,
            parent_id=session_span_id,
            root_name="invoke_agent subagent",
            session_attributes={
                "gen_ai.conversation.id": sub.session_id,
                "observal.parent_session.id": session.session_id,
                "observal.spawned_by": sub.spawned_by,
            },
        )

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attributes({"service.name": session.harness or "observal", "observal.export": True})
                },
                "scopeSpans": [{"scope": {"name": SCOPE_NAME}, "spans": writer.spans}],
            }
        ]
    }
