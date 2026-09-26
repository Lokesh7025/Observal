<!-- SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com> -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenTelemetry export

Observal can export any stored session as an OpenTelemetry trace, so you can
analyse agent sessions in Langfuse, LangSmith, Arize Phoenix, Jaeger, or any
backend behind an OpenTelemetry Collector.

Sessions are still stored as raw transcript lines. The trace is built when you
export it, from the same parsers that power the web trace viewer. Parser fixes
therefore improve past sessions too, and ingestion integrity checks keep
working on the original lines.

## Export from the CLI

Print one session as an OTLP/JSON request:

```bash
observal ops export-trace <session-id>
```

Write the most recent sessions to a file:

```bash
observal ops export-trace --recent 20 --file traces.json
```

Push straight to an OTLP/HTTP endpoint. `/v1/traces` is appended unless the
URL already ends with it, matching `OTEL_EXPORTER_OTLP_ENDPOINT`. Each session
is sent as its own request.

```bash
observal ops export-trace --recent 20 --include-content \
  --endpoint https://cloud.langfuse.com/api/public/otel \
  --header "Authorization=Basic $LANGFUSE_AUTH"
```

| Flag | Purpose |
| --- | --- |
| `SESSION_ID...` | Sessions to export |
| `--recent N` | Also export the N most recent sessions (same list as `observal ops traces`) |
| `--include-content` | Include prompts, responses and tool input/output |
| `--file PATH` | Write the combined OTLP/JSON request to a file |
| `--endpoint URL` | Push to an OTLP/HTTP traces endpoint |
| `--header KEY=VALUE` | Header for `--endpoint`, repeatable |
| `--output json` | Print the export summary as JSON |

Pass credentials through environment variables as in the examples, so they do
not end up in your shell history. Header values are never printed.

## Export from the API

```
GET /api/v1/sessions/{session_id}/otlp?include_content=false
```

The response is an OTLP `ExportTraceServiceRequest` in the OTLP/JSON encoding
and can be POSTed unchanged to any `/v1/traces` endpoint. Access follows the
session detail endpoint: users export their own sessions, and admins with
trace access export any session.

## Destinations

| Backend | `--endpoint` | `--header` |
| --- | --- | --- |
| Langfuse Cloud (EU) | `https://cloud.langfuse.com/api/public/otel` | `Authorization=Basic <base64 of public_key:secret_key>` |
| Langfuse Cloud (US) | `https://us.cloud.langfuse.com/api/public/otel` | same as above |
| LangSmith | `https://api.smith.langchain.com/otel` | `x-api-key=<key>`, optionally `Langsmith-Project=<project>` |
| OpenTelemetry Collector, Jaeger, Phoenix | `http://<host>:4318` | as configured |

The export uses the OTLP/HTTP JSON encoding. If a receiver only accepts
protobuf, send the export to an OpenTelemetry Collector and let the Collector
forward it.

## Trace shape

Each session becomes one trace:

```
invoke_agent <agent or harness>     the session
  turn N                            one per user prompt
    chat <model>                    one per model response
    execute_tool <tool>             one per tool call
  invoke_agent subagent             subagent sessions, same trace
```

| Span | Key attributes |
| --- | --- |
| Session | `session.id`, `gen_ai.conversation.id`, `user.id`, `gen_ai.agent.id`, `gen_ai.agent.name`, `observal.harness`, `observal.agent.version` |
| Model call | `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.response.finish_reasons` |
| Tool call | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`; error status when the tool reported failure |
| Content (opt-in) | `input.value`, `output.value`, and `observal.reasoning` for model thinking |

Attributes follow the OpenTelemetry GenAI semantic conventions. Spans also
carry `langfuse.observation.type` and `langsmith.span.kind`, so Langfuse and
LangSmith classify them as agent, generation, and tool runs. The session ID is
set as `langsmith.metadata.session_id`, which groups the session as a LangSmith
thread. Session-level records such as attachments and hook lifecycle events
become span events.

## Limitations

* **Durations are derived.** Transcripts record one timestamp per record, not
  start and end times. A model call runs from the previous record to its
  response, and a tool call runs from the call to its result. Waterfalls are
  accurate to the transcript, not to API latency.
* **Model input is not reconstructed.** A model call's `output.value` is the
  response. The full prompt context sent to the model is not rebuilt.
* **Stable IDs.** Trace and span IDs are derived from the session, so a
  re-export reuses them. Backends that upsert by span ID update in place;
  append-only stores such as Jaeger keep both copies.
* **Content is redacted and capped.** Secrets are redacted at ingest, and each
  content attribute is capped at 32,000 characters.
