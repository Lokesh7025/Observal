# Phase 3 & 4 Development Checkpoint

## Status: COMPLETE — BUGS FIXED

## Phase 3: Agent Registry
- [x] Database models — `observal-server/models/agent.py`
- [x] Model registry — `observal-server/models/__init__.py` (updated)
- [x] Pydantic schemas — `observal-server/schemas/agent.py`
- [x] API routes — `observal-server/api/routes/agent.py`
- [x] Agent config generator — `observal-server/services/agent_config_generator.py`
- [x] CLI commands — `observal_cli/main.py` (agent create/list/show/install)
- [x] Docs — `docs/phase-3-4-test-plan.md`
- [x] Tests — `tests/test_phase_3_4.sh`

## Phase 4: Hooks & Telemetry
- [x] ClickHouse service — `observal-server/services/clickhouse.py`
- [x] Telemetry schemas — `observal-server/schemas/telemetry.py`
- [x] Telemetry API routes — `observal-server/api/routes/telemetry.py`
- [x] CLI commands — `observal_cli/main.py` (telemetry status/test)
- [x] Main.py updated — registered agent + telemetry routers, ClickHouse init on startup

## Bug Fixes Applied (from subagent review)
- [x] **BUG 1 (Critical):** `_agent_to_response` — Pydantic validation failed because `AgentMcpLink` has no `mcp_name`. Fixed by building dict manually, excluding mcp_links from model_validate.
- [x] **BUG 2 (Critical):** SQL injection in `insert_tool_call` and `insert_agent_interaction` — user strings were f-string interpolated into SQL. Fixed with ClickHouse parameterized queries.
- [x] **BUG 3 (Critical):** ClickHouse URL construction dropped database name. Fixed with proper URL parsing and `?database=` query param.
- [x] **BUG 4 (High):** No error handling on ClickHouse inserts crashed entire requests. Fixed with try/except per event + error count in response.
- [x] **BUG 5 (High):** `install_agent` accessed `mcp_listing` relationship after commit, causing MissingGreenlet in async. Fixed by generating config before commit.
- [x] **BUG 6 (Medium):** Stray no-op query in `update_agent`. Removed.
- [x] **BUG 7 (Medium):** `AgentUpdateRequest` had no min_length on description/prompt. Added constraints.
- [x] **BUG 8 (Medium):** No cascade on agent relationships. Added `cascade="all, delete-orphan"`.
- [x] **BUG 9 (Medium):** `init_clickhouse` silently swallowed errors. Added logging.
- [x] **BUG 10 (Medium):** New httpx client per request. Fixed with shared module-level client.
- [x] **BUG 11 (Low):** Timestamp format mismatch for ClickHouse. Fixed with explicit `strftime`.

## Files Created
- `observal-server/models/agent.py`
- `observal-server/schemas/agent.py`
- `observal-server/api/routes/agent.py`
- `observal-server/services/agent_config_generator.py`
- `observal-server/services/clickhouse.py`
- `observal-server/schemas/telemetry.py`
- `observal-server/api/routes/telemetry.py`
- `docs/phase-3-4-test-plan.md`
- `docs/phase-3-4-checkpoint.md`
- `tests/test_phase_3_4.sh`

## Files Modified
- `observal-server/models/__init__.py` — added agent models
- `observal-server/main.py` — added agent + telemetry routers, ClickHouse init
- `observal_cli/main.py` — added agent + telemetry CLI commands

## Deferred (per dev plan — thin IDE adapter layers)
- Hook scripts for individual IDEs (claude_code, kiro, gemini_cli adapters)
- observal-collector Python package (shared library)
- Config generator hook injection
- VS Code/Cursor extension for telemetry
