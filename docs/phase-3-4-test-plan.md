# Phase 3 & 4 — Test Plan

---

## Prerequisites

1. Stack running: `cd docker && docker compose up -d`
2. Phase 1 & 2 passing (admin user exists, at least one approved MCP listing)
3. CLI installed: `uv pip install -e .`

---

## Phase 3: Agent Registry Tests

### Setup
Use admin API key. Need at least one approved MCP listing from Phase 2.

### T3.1 — Create Agent (Valid)
```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{
    "name": "test-agent",
    "version": "1.0.0",
    "description": "<100+ char description for the test agent that is long enough to pass the minimum length validation requirement for agent descriptions>",
    "owner": "Platform Team",
    "prompt": "<50+ char prompt — You are a test agent. Analyze the input and produce structured output with sections.>",
    "model_name": "claude-sonnet-4",
    "model_config_json": {"max_tokens": 4096, "temperature": 0.2},
    "supported_ides": ["cursor", "kiro", "claude-code"],
    "mcp_server_ids": ["<approved_mcp_id>"],
    "goal_template": {
      "description": "Analyze input and produce structured output",
      "sections": [
        {"name": "Analysis", "grounding_required": true},
        {"name": "Recommendations", "grounding_required": false}
      ]
    }
  }'
```
Expected: 200, returns agent with id, status=active, mcp_links, goal_template

### T3.2 — Create Agent (Description Too Short)
```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{
    "name": "bad-agent",
    "version": "1.0.0",
    "description": "Too short",
    "owner": "Team",
    "prompt": "You are a test agent that does things for testing purposes and more.",
    "model_name": "claude-sonnet-4",
    "goal_template": {"description": "Test", "sections": [{"name": "Output"}]}
  }'
```
Expected: 422 validation error (description min_length=100)

### T3.3 — Create Agent (No Goal Sections)
```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{
    "name": "bad-agent",
    "version": "1.0.0",
    "description": "<100+ char description>",
    "owner": "Team",
    "prompt": "<50+ char prompt>",
    "model_name": "claude-sonnet-4",
    "goal_template": {"description": "Test", "sections": []}
  }'
```
Expected: 422 validation error (sections min_length=1)

### T3.4 — Create Agent (Invalid MCP Reference)
```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{
    "name": "bad-agent",
    "version": "1.0.0",
    "description": "<100+ char description>",
    "owner": "Team",
    "prompt": "<50+ char prompt>",
    "model_name": "claude-sonnet-4",
    "mcp_server_ids": ["00000000-0000-0000-0000-000000000000"],
    "goal_template": {"description": "Test", "sections": [{"name": "Output"}]}
  }'
```
Expected: 400 "MCP server ... not found or not approved"

### T3.5 — List Agents
```bash
curl http://localhost:8000/api/v1/agents
```
Expected: 200, array containing the agent from T3.1

### T3.6 — List Agents with Search
```bash
curl "http://localhost:8000/api/v1/agents?search=test"
```
Expected: 200, returns matching agents

### T3.7 — Show Agent Detail
```bash
curl http://localhost:8000/api/v1/agents/<agent_id>
```
Expected: 200, full agent with mcp_links and goal_template sections

### T3.8 — Update Agent
```bash
curl -X PUT http://localhost:8000/api/v1/agents/<agent_id> \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"version": "1.1.0"}'
```
Expected: 200, version updated to "1.1.0"

### T3.9 — Install Agent (Cursor)
```bash
curl -X POST http://localhost:8000/api/v1/agents/<agent_id>/install \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"ide": "cursor"}'
```
Expected: 200, config_snippet with rules_file and mcp_config

### T3.10 — Install Agent (Kiro)
```bash
curl -X POST http://localhost:8000/api/v1/agents/<agent_id>/install \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"ide": "kiro"}'
```
Expected: 200, config_snippet with .kiro/rules path and mcp_json

### T3.11 — Install Agent (Claude Code)
```bash
curl -X POST http://localhost:8000/api/v1/agents/<agent_id>/install \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"ide": "claude-code"}'
```
Expected: 200, config_snippet with .claude/rules path

### T3.12 — Install Agent (Gemini CLI)
```bash
curl -X POST http://localhost:8000/api/v1/agents/<agent_id>/install \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"ide": "gemini-cli"}'
```
Expected: 200, config_snippet with GEMINI.md path

### T3.13 — Install Archived Agent
Archive an agent (direct DB or future endpoint), then:
```bash
curl -X POST http://localhost:8000/api/v1/agents/<archived_agent_id>/install \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"ide": "cursor"}'
```
Expected: 404 "Active agent not found"

---

## Phase 4: Hooks & Telemetry Tests

### T4.1 — Telemetry Status (Empty)
```bash
curl http://localhost:8000/api/v1/telemetry/status \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, `{"tool_call_events": 0, "agent_interaction_events": 0, "status": "ok"}`

### T4.2 — Ingest Tool Call Event
```bash
curl -X POST http://localhost:8000/api/v1/telemetry/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{
    "tool_calls": [{
      "mcp_server_id": "test-mcp",
      "tool_name": "get_issue",
      "input_params": "{\"id\": 123}",
      "response": "{\"title\": \"Bug\"}",
      "latency_ms": 234,
      "status": "success",
      "user_action": "accepted",
      "session_id": "sess-001",
      "ide": "cursor"
    }]
  }'
```
Expected: 200, `{"ingested": 1}`

### T4.3 — Ingest Agent Interaction Event
```bash
curl -X POST http://localhost:8000/api/v1/telemetry/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{
    "agent_interactions": [{
      "agent_id": "test-agent",
      "session_id": "sess-001",
      "tool_calls": 3,
      "user_action": "accepted",
      "latency_ms": 1500,
      "ide": "cursor"
    }]
  }'
```
Expected: 200, `{"ingested": 1}`

### T4.4 — Ingest Batch (Mixed)
```bash
curl -X POST http://localhost:8000/api/v1/telemetry/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{
    "tool_calls": [
      {"mcp_server_id": "mcp-a", "tool_name": "tool1", "status": "success", "latency_ms": 100, "ide": "kiro"},
      {"mcp_server_id": "mcp-b", "tool_name": "tool2", "status": "error", "latency_ms": 500, "ide": "kiro"}
    ],
    "agent_interactions": [
      {"agent_id": "agent-x", "tool_calls": 5, "user_action": "rejected", "latency_ms": 2000, "ide": "kiro"}
    ]
  }'
```
Expected: 200, `{"ingested": 3}`

### T4.5 — Telemetry Status (After Ingestion)
```bash
curl http://localhost:8000/api/v1/telemetry/status \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, tool_call_events > 0, agent_interaction_events > 0

### T4.6 — Ingest Without Auth
```bash
curl -X POST http://localhost:8000/api/v1/telemetry/events \
  -H "Content-Type: application/json" \
  -d '{"tool_calls": [{"mcp_server_id": "x", "tool_name": "y"}]}'
```
Expected: 401 or 422

### T4.7 — Empty Batch
```bash
curl -X POST http://localhost:8000/api/v1/telemetry/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"tool_calls": [], "agent_interactions": []}'
```
Expected: 200, `{"ingested": 0}`

---

## CLI Integration Tests (Phase 3 & 4)

### T3.CLI.1 — Agent Create via CLI
```bash
observal agent create
# Fill in prompts interactively
```
Expected: Prints "Agent created! ID: <uuid>"

### T3.CLI.2 — Agent List via CLI
```bash
observal agent list
observal agent list --search test
```
Expected: Rich table with columns: ID, Name, Version, Model, Owner

### T3.CLI.3 — Agent Show via CLI
```bash
observal agent show <agent_id>
```
Expected: Prints name, version, owner, model, description, IDEs, MCP servers, goal template

### T3.CLI.4 — Agent Install via CLI
```bash
observal agent install <agent_id> --ide kiro
```
Expected: Prints config snippet JSON

### T4.CLI.1 — Telemetry Status via CLI
```bash
observal telemetry status
```
Expected: Prints status, tool call count, agent interaction count

### T4.CLI.2 — Telemetry Test via CLI
```bash
observal telemetry test
```
Expected: Prints "Test event sent! Ingested: 1"

---

## Edge Cases to Verify

| Case | Expected |
|---|---|
| Agent with no MCP servers | Should succeed (mcp_server_ids defaults to []) |
| Agent referencing unapproved MCP | 400 error |
| Update agent you don't own | 403 error |
| Install agent with unsupported IDE string | Should succeed (generates default config) |
| Very large prompt (10k+ chars) | Should succeed (Text column) |
| Concurrent agent creates | No conflicts (UUIDs) |
| Telemetry with 0 latency | Should succeed |
| Telemetry with empty strings | Should succeed |
