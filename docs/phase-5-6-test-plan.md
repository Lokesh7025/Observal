# Phase 5 & 6 — Test Plan

---

## Prerequisites

1. Stack running: `cd docker && docker compose up -d --build`
2. Phase 1-4 passing (admin user, approved MCP, agent, telemetry data)
3. CLI installed: `uv pip install -e .`

---

## Phase 5: Dashboard / Metrics API Tests

### Setup
Bootstrap admin, approved MCP, agent, and telemetry data.

### T5.1 — MCP Metrics (Empty)
```bash
curl http://localhost:8000/api/v1/mcps/<mcp_id>/metrics \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, returns McpMetrics with total_downloads >= 0, total_calls >= 0

### T5.2 — MCP Metrics (After Installs + Telemetry)
Install the MCP, send telemetry, then query metrics.
Expected: 200, total_downloads > 0, total_calls > 0

### T5.3 — Agent Metrics (Empty)
```bash
curl http://localhost:8000/api/v1/agents/<agent_id>/metrics \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, returns AgentMetrics

### T5.4 — Agent Metrics (After Installs + Telemetry)
Install the agent, send telemetry, then query metrics.
Expected: 200, total_downloads > 0, total_interactions > 0

### T5.5 — Overview Stats
```bash
curl http://localhost:8000/api/v1/overview/stats \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, total_mcps > 0, total_agents > 0, total_users > 0

### T5.6 — Top MCPs
```bash
curl http://localhost:8000/api/v1/overview/top-mcps \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array of TopItem objects

### T5.7 — Top Agents
```bash
curl http://localhost:8000/api/v1/overview/top-agents \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array of TopItem objects

### T5.8 — Trends
```bash
curl http://localhost:8000/api/v1/overview/trends \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array of {date, submissions, users}

---

## Phase 6: Feedback Tests

### T6.1 — Submit Feedback (MCP)
```bash
curl -X POST http://localhost:8000/api/v1/feedback \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"listing_id": "<mcp_id>", "listing_type": "mcp", "rating": 4, "comment": "Great tool"}'
```
Expected: 200, returns FeedbackResponse with rating=4

### T6.2 — Submit Feedback (Agent)
```bash
curl -X POST http://localhost:8000/api/v1/feedback \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"listing_id": "<agent_id>", "listing_type": "agent", "rating": 5, "comment": "Excellent"}'
```
Expected: 200, returns FeedbackResponse with rating=5

### T6.3 — Submit Feedback (Invalid Rating)
```bash
curl -X POST http://localhost:8000/api/v1/feedback \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"listing_id": "<mcp_id>", "listing_type": "mcp", "rating": 6}'
```
Expected: 422 validation error

### T6.4 — Submit Feedback (Invalid Type)
```bash
curl -X POST http://localhost:8000/api/v1/feedback \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <admin_key>" \
  -d '{"listing_id": "<mcp_id>", "listing_type": "invalid", "rating": 3}'
```
Expected: 422 validation error

### T6.5 — Get MCP Feedback
```bash
curl http://localhost:8000/api/v1/feedback/mcp/<mcp_id>
```
Expected: 200, array containing feedback from T6.1

### T6.6 — Get Agent Feedback
```bash
curl http://localhost:8000/api/v1/feedback/agent/<agent_id>
```
Expected: 200, array containing feedback from T6.2

### T6.7 — Feedback Summary
```bash
curl http://localhost:8000/api/v1/feedback/summary/<mcp_id>
```
Expected: 200, average_rating > 0, total_reviews > 0

### T6.8 — My Feedback Received
```bash
curl http://localhost:8000/api/v1/feedback/me \
  -H "X-API-Key: <admin_key>"
```
Expected: 200, array containing feedback on admin's listings

### T6.9 — Submit Feedback Without Auth
```bash
curl -X POST http://localhost:8000/api/v1/feedback \
  -H "Content-Type: application/json" \
  -d '{"listing_id": "<mcp_id>", "listing_type": "mcp", "rating": 3}'
```
Expected: 401 or 422

---

## CLI Integration Tests

### T5.CLI.1 — Metrics via CLI
```bash
observal metrics <mcp_id> --type mcp
observal metrics <agent_id> --type agent
```
Expected: Prints metrics summary

### T5.CLI.2 — Overview via CLI
```bash
observal overview
```
Expected: Prints enterprise overview stats

### T6.CLI.1 — Rate via CLI
```bash
observal rate <mcp_id> --stars 4 --comment "Nice"
```
Expected: Prints "Rated 4/5 ✓"

### T6.CLI.2 — Feedback via CLI
```bash
observal feedback <mcp_id>
```
Expected: Prints average rating and individual reviews

---

## Edge Cases

| Case | Expected |
|---|---|
| Metrics for non-existent MCP | Should return zeros |
| Feedback with rating=0 | 422 validation error |
| Feedback with no comment | Should succeed |
| Summary for listing with no feedback | average_rating=0, total_reviews=0 |
| Multiple feedback from same user | All stored (no uniqueness constraint) |
