# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for exporting stored sessions as OTLP/JSON traces."""

from __future__ import annotations

import json

import pytest

from services.otel_export import (
    MAX_CONTENT_CHARS,
    STATUS_CODE_ERROR,
    SessionTrace,
    build_otlp_request,
    to_unix_nanos,
    trace_id_for,
)
from services.session_parsers import parse_raw_events

SECOND = 1_000_000_000
T0 = to_unix_nanos("2026-09-26 10:00:00.000")

CLAUDE_LINES = [
    {
        "type": "user",
        "uuid": "u1",
        "timestamp": "2026-09-26T10:00:00.000Z",
        "message": {"role": "user", "content": "List the files"},
    },
    {
        "type": "assistant",
        "uuid": "a1",
        "parentUuid": "u1",
        "timestamp": "2026-09-26T10:00:02.000Z",
        "message": {
            "model": "claude-sonnet-5",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50},
            "content": [
                {"type": "text", "text": "Listing now."},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
            ],
        },
    },
    {
        "type": "user",
        "uuid": "u2",
        "parentUuid": "a1",
        "timestamp": "2026-09-26T10:00:05.000Z",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.md"}]},
    },
    {
        "type": "assistant",
        "uuid": "a2",
        "parentUuid": "u2",
        "timestamp": "2026-09-26T10:00:09.000Z",
        "message": {
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 130, "output_tokens": 8},
            "content": [{"type": "text", "text": "Found a.md."}],
        },
    },
]
CLAUDE_EVENT_TYPES = ["user_prompt", "tool_call", "tool_result", "assistant_text"]


def _claude_rows() -> list[dict]:
    return [
        {
            "line_offset": offset,
            "timestamp": line["timestamp"].replace("T", " ").replace("Z", ""),
            "event_type": event_type,
            "tool_id": None,
            "harness": "claude-code",
            "raw_line": json.dumps(line),
            "ingested_at": "2026-09-26 10:01:00.000",
        }
        for offset, (line, event_type) in enumerate(zip(CLAUDE_LINES, CLAUDE_EVENT_TYPES, strict=True))
    ]


def _claude_trace() -> SessionTrace:
    rows = _claude_rows()
    return SessionTrace(session_id="sess-1", harness="claude-code", rows=rows, events=parse_raw_events(rows))


def _spans(request: dict) -> list[dict]:
    [resource_spans] = request["resourceSpans"]
    [scope_spans] = resource_spans["scopeSpans"]
    return scope_spans["spans"]


def _attrs(span: dict) -> dict:
    values = {}
    for item in span["attributes"]:
        value = item["value"]
        if "arrayValue" in value:
            values[item["key"]] = [next(iter(v.values())) for v in value["arrayValue"]["values"]]
        else:
            values[item["key"]] = next(iter(value.values()))
    return values


def _by_name(spans: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for span in spans:
        grouped.setdefault(span["name"], []).append(span)
    return grouped


def _offset_seconds(value: str) -> float:
    return (int(value) - T0) / SECOND


def test_claude_session_becomes_agent_turn_chat_and_tool_spans():
    request = build_otlp_request(
        _claude_trace(), user_id="user-1", agent_id="agent-1", agent_name="reviewer", agent_version="2.0.0"
    )
    spans = _spans(request)
    named = _by_name(spans)

    assert sorted(named) == ["chat claude-sonnet-5", "execute_tool Bash", "invoke_agent reviewer", "turn 1"]
    assert len(named["chat claude-sonnet-5"]) == 2
    assert {span["traceId"] for span in spans} == {trace_id_for("sess-1")}
    assert len({span["spanId"] for span in spans}) == len(spans)

    [session] = named["invoke_agent reviewer"]
    [turn] = named["turn 1"]
    [tool] = named["execute_tool Bash"]
    first_chat, second_chat = sorted(named["chat claude-sonnet-5"], key=lambda s: int(s["startTimeUnixNano"]))

    assert "parentSpanId" not in session
    assert turn["parentSpanId"] == session["spanId"]
    assert {first_chat["parentSpanId"], second_chat["parentSpanId"], tool["parentSpanId"]} == {turn["spanId"]}

    # Model call answers the prompt; the tool runs until its result line; the
    # second model call starts once the result is back.
    assert (_offset_seconds(first_chat["startTimeUnixNano"]), _offset_seconds(first_chat["endTimeUnixNano"])) == (0, 2)
    assert (_offset_seconds(tool["startTimeUnixNano"]), _offset_seconds(tool["endTimeUnixNano"])) == (2, 5)
    assert (_offset_seconds(second_chat["startTimeUnixNano"]), _offset_seconds(second_chat["endTimeUnixNano"])) == (
        5,
        9,
    )
    assert (_offset_seconds(session["startTimeUnixNano"]), _offset_seconds(session["endTimeUnixNano"])) == (0, 9)

    session_attrs = _attrs(session)
    assert session_attrs["session.id"] == "sess-1"
    assert session_attrs["langsmith.metadata.session_id"] == "sess-1"
    assert session_attrs["user.id"] == "user-1"
    assert session_attrs["gen_ai.agent.name"] == "reviewer"
    assert session_attrs["gen_ai.operation.name"] == "invoke_agent"
    assert session_attrs["observal.harness"] == "claude-code"

    chat_attrs = _attrs(first_chat)
    assert chat_attrs["gen_ai.operation.name"] == "chat"
    assert chat_attrs["langfuse.observation.type"] == "generation"
    assert chat_attrs["gen_ai.request.model"] == "claude-sonnet-5"
    assert chat_attrs["gen_ai.usage.input_tokens"] == "100"
    assert chat_attrs["gen_ai.usage.output_tokens"] == "20"
    assert chat_attrs["gen_ai.usage.cache_read.input_tokens"] == "50"
    assert chat_attrs["gen_ai.usage.total_tokens"] == "120"
    assert chat_attrs["gen_ai.response.finish_reasons"] == ["tool_use"]

    tool_attrs = _attrs(tool)
    assert tool_attrs["gen_ai.tool.name"] == "Bash"
    assert tool_attrs["gen_ai.tool.call.id"] == "toolu_1"
    assert tool_attrs["langfuse.observation.type"] == "tool"
    assert "status" not in tool

    resource = {item["key"]: item["value"] for item in request["resourceSpans"][0]["resource"]["attributes"]}
    assert resource["service.name"] == {"stringValue": "claude-code"}


def test_content_is_excluded_unless_requested():
    without = _spans(build_otlp_request(_claude_trace()))
    content_keys = {"input.value", "output.value", "observal.reasoning"}
    assert not any(content_keys & set(_attrs(span)) for span in without)

    named = _by_name(_spans(build_otlp_request(_claude_trace(), include_content=True)))
    [turn] = named["turn 1"]
    [tool] = named["execute_tool Bash"]
    [session] = named["invoke_agent claude-code"]
    assert _attrs(turn)["input.value"] == "List the files"
    assert _attrs(turn)["output.value"] == "Found a.md."
    assert json.loads(_attrs(tool)["input.value"]) == {"command": "ls"}
    assert _attrs(tool)["output.value"] == "a.md"
    assert _attrs(session)["input.value"] == "List the files"
    assert _attrs(session)["output.value"] == "Found a.md."


def test_export_is_deterministic_so_reexports_overwrite():
    assert build_otlp_request(_claude_trace()) == build_otlp_request(_claude_trace())


def test_tool_only_response_lists_its_tool_calls_as_output():
    events = [
        {"timestamp": "2026-09-26 10:00:00.000", "event_name": "hook_userpromptsubmit", "attributes": {}},
        {
            "timestamp": "2026-09-26 10:00:01.000",
            "event_name": "hook_pretooluse",
            "attributes": {"tool_name": "shell", "tool_input": '{"cmd": "ls"}', "tool_use_id": "c1"},
        },
    ]
    trace = SessionTrace(session_id="s", harness="codex", rows=[], events=events)
    [chat] = _by_name(_spans(build_otlp_request(trace, include_content=True)))["chat"]
    assert json.loads(_attrs(chat)["output.value"]) == [{"name": "shell", "arguments": {"cmd": "ls"}}]


def test_separate_result_records_close_tools_and_split_model_calls():
    def event(second: int, name: str, **attributes: str) -> dict:
        return {"timestamp": f"2026-09-26 10:00:{second:02d}.000", "event_name": name, "attributes": attributes}

    events = [
        event(0, "hook_userpromptsubmit", tool_input="fix it"),
        event(1, "hook_pretooluse", tool_name="shell", tool_input="ls", tool_use_id="c1"),
        event(2, "hook_posttooluse", tool_name="shell", tool_response="ok", tool_use_id="c1"),
        event(3, "hook_pretooluse", tool_name="apply_patch", tool_input="diff", tool_use_id="c2"),
        event(4, "hook_posttooluse", tool_name="apply_patch", tool_use_id="c2", success="false"),
        event(5, "hook_assistant_response", tool_response="Could not patch."),
        event(5, "hook_token_usage", input_tokens="40", output_tokens="7"),
    ]
    trace = SessionTrace(session_id="codex-1", harness="codex", rows=[], events=events)
    named = _by_name(_spans(build_otlp_request(trace, include_content=True)))

    chats = sorted(named["chat"], key=lambda s: int(s["startTimeUnixNano"]))
    assert [(_offset_seconds(s["startTimeUnixNano"]), _offset_seconds(s["endTimeUnixNano"])) for s in chats] == [
        (0, 1),
        (2, 3),
        (4, 5),
    ]
    assert _attrs(chats[2])["gen_ai.usage.input_tokens"] == "40"
    assert _attrs(chats[2])["output.value"] == "Could not patch."

    [shell] = named["execute_tool shell"]
    [patch] = named["execute_tool apply_patch"]
    assert (_offset_seconds(shell["startTimeUnixNano"]), _offset_seconds(shell["endTimeUnixNano"])) == (1, 2)
    assert _attrs(shell)["output.value"] == "ok"
    assert (_offset_seconds(patch["startTimeUnixNano"]), _offset_seconds(patch["endTimeUnixNano"])) == (3, 4)
    assert patch["status"] == {"code": STATUS_CODE_ERROR}


def test_tool_end_prefers_result_row_with_matching_tool_id():
    events = [
        {"timestamp": "2026-09-26 10:00:00.000", "event_name": "hook_userpromptsubmit", "attributes": {}},
        {
            "timestamp": "2026-09-26 10:00:01.000",
            "event_name": "hook_posttooluse",
            "attributes": {"tool_name": "read", "tool_use_id": "t1"},
        },
    ]
    rows = [
        {"event_type": "tool_result", "timestamp": "2026-09-26 10:00:02.000", "tool_id": "other"},
        {"event_type": "tool_result", "timestamp": "2026-09-26 10:00:06.000", "tool_id": "t1"},
    ]
    trace = SessionTrace(session_id="s", harness="kiro", rows=rows, events=events)
    [tool] = _by_name(_spans(build_otlp_request(trace)))["execute_tool read"]
    assert _offset_seconds(tool["endTimeUnixNano"]) == 6


def test_parallel_tool_calls_each_claim_their_own_result_row():
    events = [
        {"timestamp": "2026-09-26 10:00:00.000", "event_name": "hook_userpromptsubmit", "attributes": {}},
        {
            "timestamp": "2026-09-26 10:00:01.000",
            "event_name": "hook_posttooluse",
            "attributes": {"tool_name": "Read", "tool_use_id": "a"},
        },
        {
            "timestamp": "2026-09-26 10:00:01.000",
            "event_name": "hook_posttooluse",
            "attributes": {"tool_name": "Grep", "tool_use_id": "b"},
        },
        {
            "timestamp": "2026-09-26 10:00:01.000",
            "event_name": "hook_posttooluse",
            "attributes": {"tool_name": "Glob", "tool_use_id": "c"},
        },
    ]
    # Claude Code result rows carry no tool ID; one row is written per result.
    rows = [
        {"event_type": "tool_result", "timestamp": "2026-09-26 10:00:02.000", "tool_id": None},
        {"event_type": "tool_result", "timestamp": "2026-09-26 10:00:04.000", "tool_id": "c"},
        {"event_type": "tool_result", "timestamp": "2026-09-26 10:00:03.000", "tool_id": None},
    ]
    trace = SessionTrace(session_id="s", harness="claude-code", rows=rows, events=events)
    named = _by_name(_spans(build_otlp_request(trace)))
    ends = {name: _offset_seconds(spans[0]["endTimeUnixNano"]) for name, spans in named.items()}
    assert ends["execute_tool Read"] == 2
    assert ends["execute_tool Grep"] == 3
    assert ends["execute_tool Glob"] == 4


def test_unmatched_tool_has_zero_duration_and_orphan_result_becomes_its_own_span():
    events = [
        {"timestamp": "2026-09-26 10:00:03.000", "event_name": "tool_call", "attributes": {"tool_name": "grep"}},
        {"timestamp": "2026-09-26 10:00:04.000", "event_name": "tool_result", "attributes": {"tool_name": "glob"}},
    ]
    trace = SessionTrace(session_id="s", harness="codex", rows=[], events=events)
    named = _by_name(_spans(build_otlp_request(trace)))
    [grep] = named["execute_tool grep"]
    [glob] = named["execute_tool glob"]
    assert grep["startTimeUnixNano"] == grep["endTimeUnixNano"]
    assert glob["startTimeUnixNano"] == glob["endTimeUnixNano"]
    # Both land in an implicit turn because no prompt preceded them.
    assert set(named) >= {"turn 1"}


def test_other_events_become_span_events_with_body_only_when_content_is_included():
    events = [
        {"timestamp": "2026-09-26 10:00:00.000", "event_name": "hook_sessionstart", "body": "boot", "attributes": {}},
        {"timestamp": "2026-09-26 10:00:01.000", "event_name": "hook_userpromptsubmit", "attributes": {}},
        {"timestamp": "2026-09-26 10:00:02.000", "event_name": "attachment", "body": "a.png", "attributes": {}},
    ]
    trace = SessionTrace(session_id="s", harness="claude-code", rows=[], events=events)

    named = _by_name(_spans(build_otlp_request(trace)))
    [session] = named["invoke_agent claude-code"]
    [turn] = named["turn 1"]
    assert [e["name"] for e in session["events"]] == ["hook_sessionstart"]
    assert [e["name"] for e in turn["events"]] == ["attachment"]
    assert turn["events"][0]["attributes"] == []

    [turn] = _by_name(_spans(build_otlp_request(trace, include_content=True)))["turn 1"]
    assert turn["events"][0]["attributes"] == [{"key": "observal.event.body", "value": {"stringValue": "a.png"}}]


def test_subagents_nest_under_the_parent_session_in_the_same_trace():
    child_events = [
        {"timestamp": "2026-09-26 10:00:03.000", "event_name": "hook_userpromptsubmit", "attributes": {}},
        {"timestamp": "2026-09-26 10:00:04.000", "event_name": "hook_assistant_response", "attributes": {}},
    ]
    child = SessionTrace(session_id="child-1", harness="claude-code", rows=[], events=child_events, spawned_by="a1")
    spans = _spans(build_otlp_request(_claude_trace(), subagents=[child]))
    named = _by_name(spans)

    [parent] = named["invoke_agent claude-code"]
    [sub] = named["invoke_agent subagent"]
    assert sub["parentSpanId"] == parent["spanId"]
    assert {span["traceId"] for span in spans} == {trace_id_for("sess-1")}
    assert _attrs(sub)["observal.session.id"] == "child-1"
    assert _attrs(sub)["observal.spawned_by"] == "a1"
    assert len({span["spanId"] for span in spans}) == len(spans)


def test_long_content_is_truncated():
    events = [
        {
            "timestamp": "2026-09-26 10:00:00.000",
            "event_name": "hook_userpromptsubmit",
            "attributes": {"tool_input": "x" * (MAX_CONTENT_CHARS + 10)},
        }
    ]
    trace = SessionTrace(session_id="s", harness="claude-code", rows=[], events=events)
    [turn] = _by_name(_spans(build_otlp_request(trace, include_content=True)))["turn 1"]
    value = _attrs(turn)["input.value"]
    assert value.endswith("...[truncated]")
    assert len(value) == MAX_CONTENT_CHARS + len("...[truncated]")


def test_session_without_parseable_events_still_exports_a_root_span():
    rows = [{"event_type": "meta", "timestamp": "2026-09-26 10:00:07.000"}]
    trace = SessionTrace(session_id="s", harness="cursor", rows=rows, events=[])
    [root] = _spans(build_otlp_request(trace))
    assert root["name"] == "invoke_agent cursor"
    assert _offset_seconds(root["startTimeUnixNano"]) == 7


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-26 10:00:00.000", T0),
        ("2026-09-26T10:00:00Z", T0),
        ("2026-09-26T10:00:00.000+00:00", T0),
        ("2026-09-26T15:30:00.000+05:30", T0),
        ("2026-09-26T10:00:00.123456789Z", T0 + 123_456_000),
        (T0 // 1_000_000, T0),
        (str(T0 // 1_000_000_000), T0),
        ("1970-01-01 00:00:00.000", None),
        ("not a time", None),
        ("", None),
        (None, None),
        (0, None),
    ],
)
def test_to_unix_nanos(value, expected):
    assert to_unix_nanos(value) == expected
