# Phase 7 & 8 — Test Plan

---

## Prerequisites

1. Stack running: `cd docker && docker compose up -d --build`
2. Admin user, approved MCP, agent, and telemetry data seeded
3. CLI installed: `uv pip install -e .`

---

## Phase 7: Eval Engine Tests

### T7.1 — Run Evaluation (With Traces)
```bash
curl -X POST http://localhost:8000/api/v1/eval/agents/<agent_id> \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{}'
```
Expected: 200, returns EvalRunDetailResponse with status=completed, traces_evaluated > 0, scorecards array

### T7.2 — Run Evaluation (No Traces)
Create a new agent with no telemetry, then evaluate.
Expected: 200, status=completed, traces_evaluated=0, scorecards=[]

### T7.3 — Run Evaluation (Specific Trace)
```bash
curl -X POST http://localhost:8000/api/v1/eval/agents/<agent_id> \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"trace_id": "<event_id>"}'
```
Expected: 200, traces_evaluated <= 1

### T7.4 — List Eval Runs
```bash
curl http://localhost:8000/api/v1/eval/agents/<agent_id>/runs \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array of EvalRunResponse

### T7.5 — List Scorecards
```bash
curl http://localhost:8000/api/v1/eval/agents/<agent_id>/scorecards \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array of ScorecardResponse with dimensions

### T7.6 — List Scorecards (Filter by Version)
```bash
curl "http://localhost:8000/api/v1/eval/agents/<agent_id>/scorecards?version=1.0.0" \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, filtered results

### T7.7 — Get Scorecard Detail
```bash
curl http://localhost:8000/api/v1/eval/scorecards/<scorecard_id> \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, full scorecard with dimensions array (5 dimensions)

### T7.8 — Compare Versions
```bash
curl "http://localhost:8000/api/v1/eval/agents/<agent_id>/compare?version_a=1.0.0&version_b=2.0.0" \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, {version_a: {avg_score, count}, version_b: {avg_score, count}}

### T7.9 — Evaluate Non-Existent Agent
```bash
curl -X POST http://localhost:8000/api/v1/eval/agents/00000000-0000-0000-0000-000000000000 \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{}'
```
Expected: 404

### T7.10 — Get Non-Existent Scorecard
```bash
curl http://localhost:8000/api/v1/eval/scorecards/00000000-0000-0000-0000-000000000000 \
  -H "X-API-Key: <admin_key>"
```
Expected: 404

---

## Phase 8: Admin API Tests

### T8.1 — List Settings (Empty)
```bash
curl http://localhost:8000/api/v1/admin/settings \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, empty array

### T8.2 — Create Setting
```bash
curl -X PUT http://localhost:8000/api/v1/admin/settings/feedback_visibility \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"value": "public"}'
```
Expected: 200, returns {key: "feedback_visibility", value: "public"}

### T8.3 — Get Setting
```bash
curl http://localhost:8000/api/v1/admin/settings/feedback_visibility \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, returns the setting

### T8.4 — Update Setting
```bash
curl -X PUT http://localhost:8000/api/v1/admin/settings/feedback_visibility \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"value": "private"}'
```
Expected: 200, value updated to "private"

### T8.5 — List Settings (After Create)
```bash
curl http://localhost:8000/api/v1/admin/settings \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array with at least 1 item

### T8.6 — Delete Setting
```bash
curl -X DELETE http://localhost:8000/api/v1/admin/settings/feedback_visibility \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, {deleted: "feedback_visibility"}

### T8.7 — Get Deleted Setting
Expected: 404

### T8.8 — List Users
```bash
curl http://localhost:8000/api/v1/admin/users \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array with at least 1 user

### T8.9 — Update User Role
```bash
curl -X PUT http://localhost:8000/api/v1/admin/users/<user_id>/role \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"role": "developer"}'
```
Expected: 200, role updated

### T8.10 — Update User Role (Invalid)
```bash
curl -X PUT http://localhost:8000/api/v1/admin/users/<user_id>/role \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"role": "superadmin"}'
```
Expected: 422

### T8.11 — Non-Admin Access
Expected: 403 on all admin endpoints

---

## Edge Cases

| Case | Expected |
|---|---|
| Eval with no LLM configured | Falls back to heuristic scoring |
| Scorecard dimensions always 5 | task_completion, tool_usage_efficiency, response_quality, factual_grounding, user_satisfaction |
| Compare with non-existent version | Returns avg_score=0, count=0 |
| Admin setting key with special chars | Should work (String column) |
| Delete non-existent setting | 404 |
