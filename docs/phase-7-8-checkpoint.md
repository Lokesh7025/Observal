# Phase 7 & 8 Development Checkpoint

## Status: COMPLETE — BUGS FIXED

## Phase 7: Eval Engine (SLM-as-a-Judge)
- [x] Database models — `models/eval.py` (EvalRun, Scorecard, ScorecardDimension)
- [x] Pydantic schemas — `schemas/eval.py`
- [x] EvalModel abstraction — OpenAI-compatible API call in `services/eval_service.py`
- [x] Heuristic fallback — when no LLM configured, scores based on acceptance/latency/tool calls
- [x] Judge prompt template — structured JSON output with 5 dimensions
- [x] Evaluation pipeline — fetch traces from ClickHouse, evaluate, store scorecards
- [x] API routes — `api/routes/eval.py` (run eval, list runs, list/get scorecards, compare versions)
- [x] CLI commands — `observal eval run/scorecards/show/compare`
- [x] Config — EVAL_MODEL_URL, EVAL_MODEL_API_KEY, EVAL_MODEL_NAME in Settings

## Phase 8: Web UI Backend
- [x] Admin settings CRUD — `api/routes/admin.py` (list, get, upsert, delete)
- [x] User management — list users, update roles
- [x] Self-demotion protection — admin cannot demote themselves
- [x] Schemas — `schemas/admin.py`
- [x] CLI commands — `observal admin settings/set/users`

## Bug Fixes Applied (from subagent review)
- [x] **BUG (High):** `admin_set` CLI always failed — dead `client.post()` call before PUT. Removed.
- [x] **BUG (High):** Admin self-demotion lockout — added guard preventing self-role-change.
- [x] **BUG (Medium):** Missing cascade on EvalRun→Scorecard. Added `cascade="all, delete-orphan"` + `ondelete="CASCADE"`.
- [x] **BUG (Medium):** Fallback scorecard scores not clamped to [0,10]. Fixed with min/max.
- [x] **BUG (Medium):** EvalRun.scorecards eager-loaded unnecessarily on list. Changed to `lazy="noload"`.
- [x] **BUG (Medium):** `eval_show` CLI format specs crash on None. Fixed with `.get()` defaults.
- [x] **BUG (Medium):** `admin_set` inconsistent error handling. Added try/except for ConnectError.
- [x] **BUG (Low):** Fragile truthiness check on float in bottleneck logic. Fixed with explicit `== 0.0`.
- [x] **BUG (Low):** Unbounded error message storage. Truncated to 2000 chars.
- [x] **BUG (Low):** Dead `import json` in admin_set. Removed.

## Files Created
- `observal-server/models/eval.py`
- `observal-server/schemas/eval.py`
- `observal-server/schemas/admin.py`
- `observal-server/services/eval_service.py`
- `observal-server/api/routes/eval.py`
- `observal-server/api/routes/admin.py`
- `docs/phase-7-8-test-plan.md`
- `docs/phase-7-8-checkpoint.md`
- `tests/test_phase_7_8.sh`

## Files Modified
- `observal-server/models/__init__.py` — added eval models
- `observal-server/config.py` — added EVAL_MODEL_* settings
- `observal-server/main.py` — added eval + admin routers
- `observal_cli/main.py` — added eval + admin CLI commands
- `observal_cli/client.py` — (fixed in Phase 5-6, still good)
