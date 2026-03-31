#!/usr/bin/env bash
# Phase 3 & 4 — Idempotent Integration Test Script
# Depends on Phase 1 & 2 being set up (admin user + approved MCP listing).
# Resets DB on each run for clean state.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
DOCKER_DIR="${DOCKER_DIR:-$(cd "$(dirname "$0")/../docker" && pwd)}"
PASS=0
FAIL=0
SKIP=0
FAILURES=""

# ── Helpers ──────────────────────────────────────────────────────────────────

green()  { printf "\033[32m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }

assert_status() {
  local test_id="$1" expected="$2" actual="$3"
  if [ "$actual" -eq "$expected" ]; then
    green "  ✓ $test_id — HTTP $actual"
    PASS=$((PASS + 1))
  else
    red "  ✗ $test_id — HTTP $actual (expected $expected)"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES\n  ✗ $test_id"
  fi
}

assert_status_oneof() {
  local test_id="$1" actual="$2"
  shift 2
  for expected in "$@"; do
    if [ "$actual" -eq "$expected" ]; then
      green "  ✓ $test_id — HTTP $actual"
      PASS=$((PASS + 1))
      return
    fi
  done
  red "  ✗ $test_id — HTTP $actual (expected one of $*)"
  FAIL=$((FAIL + 1))
  FAILURES="$FAILURES\n  ✗ $test_id"
}

assert_json_field() {
  local test_id="$1" body="$2" field="$3" expected="$4"
  local actual
  actual=$(echo "$body" | jq -r "$field" 2>/dev/null || echo "__jq_error__")
  if [ "$actual" = "$expected" ]; then
    green "  ✓ $test_id — $field = $expected"
    PASS=$((PASS + 1))
  else
    red "  ✗ $test_id — $field = '$actual' (expected '$expected')"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES\n  ✗ $test_id"
  fi
}

assert_json_nonempty() {
  local test_id="$1" body="$2" field="$3"
  local actual
  actual=$(echo "$body" | jq -r "$field" 2>/dev/null || echo "")
  if [ -n "$actual" ] && [ "$actual" != "null" ] && [ "$actual" != "" ]; then
    green "  ✓ $test_id — $field is present"
    PASS=$((PASS + 1))
  else
    red "  ✗ $test_id — $field is empty/null"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES\n  ✗ $test_id"
  fi
}

assert_json_gt() {
  local test_id="$1" body="$2" field="$3" threshold="$4"
  local actual
  actual=$(echo "$body" | jq -r "$field" 2>/dev/null || echo "0")
  if [ "$actual" -gt "$threshold" ] 2>/dev/null; then
    green "  ✓ $test_id — $field = $actual (> $threshold)"
    PASS=$((PASS + 1))
  else
    red "  ✗ $test_id — $field = $actual (expected > $threshold)"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES\n  ✗ $test_id"
  fi
}

curl_get() {
  local url="$1"; shift
  curl -s -w "\n%{http_code}" "$url" "$@"
}

curl_post() {
  local url="$1" data="$2"; shift 2
  curl -s -w "\n%{http_code}" -X POST "$url" \
    -H "Content-Type: application/json" \
    -d "$data" "$@"
}

curl_put() {
  local url="$1" data="$2"; shift 2
  curl -s -w "\n%{http_code}" -X PUT "$url" \
    -H "Content-Type: application/json" \
    -d "$data" "$@"
}

parse_response() {
  local raw="$1"
  STATUS=$(echo "$raw" | tail -1)
  BODY=$(echo "$raw" | sed '$d')
}

wait_for_server() {
  bold "  ⏳ Waiting for server at $BASE_URL ..."
  for i in $(seq 1 30); do
    if curl -sf "$BASE_URL/health" > /dev/null 2>&1; then
      green "  Server is up!"
      return
    fi
    if [ "$i" -eq 30 ]; then red "  Server not reachable after 30s. Aborting."; exit 1; fi
    sleep 1
  done
}

# ── Bootstrap: reset DB and create admin + approved MCP ──────────────────────

bold "═══ Bootstrap: Reset & Setup ═══"

# Reset DB for clean state
yellow "  ↻ Resetting stack..."
docker compose -f "$DOCKER_DIR/docker-compose.yml" down -v > /dev/null 2>&1
docker compose -f "$DOCKER_DIR/docker-compose.yml" up -d > /dev/null 2>&1
wait_for_server

# Init admin
parse_response "$(curl_post "$BASE_URL/api/v1/auth/init" \
  '{"email":"testadmin@observal.dev","name":"Test Admin"}')"
if [ "$STATUS" -ne 200 ]; then
  red "  Failed to init admin (HTTP $STATUS). Aborting."
  exit 1
fi
API_KEY=$(echo "$BODY" | jq -r '.api_key')
if [ -z "$API_KEY" ] || [ "$API_KEY" = "null" ]; then
  red "  API key is null. Aborting."
  exit 1
fi
green "  Admin initialized."
AUTH=(-H "X-API-Key: $API_KEY")

LONG_DESC="This is a comprehensive test MCP server for integration testing purposes. It provides various utility tools and demonstrates the full lifecycle of MCP registration and validation."
AGENT_DESC="This is a comprehensive test agent for integration testing purposes. It analyzes input data and produces structured output with multiple sections for validation."
AGENT_PROMPT="You are a test agent for integration testing. Analyze the input provided and produce structured output with clear sections. Always cite sources and provide actionable recommendations."

# Submit and approve an MCP listing for agent tests
parse_response "$(curl_post "$BASE_URL/api/v1/mcps/submit" \
  "{
    \"git_url\": \"https://github.com/example/test-mcp.git\",
    \"name\": \"bootstrap-mcp\",
    \"version\": \"1.0.0\",
    \"description\": \"$LONG_DESC\",
    \"category\": \"utilities\",
    \"owner\": \"Platform Team\",
    \"supported_ides\": [\"cursor\", \"kiro\"]
  }" "${AUTH[@]}")"
MCP_ID=$(echo "$BODY" | jq -r '.id')

parse_response "$(curl_post "$BASE_URL/api/v1/review/$MCP_ID/approve" '{}' "${AUTH[@]}")"
green "  MCP $MCP_ID approved."

# ══════════════════════════════════════════════════════════════════════════════
bold ""
bold "═══ Phase 3: Agent Registry Tests ═══"
# ══════════════════════════════════════════════════════════════════════════════

# T3.1 — Create Agent (Valid)
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{
    \"name\": \"test-agent\",
    \"version\": \"1.0.0\",
    \"description\": \"$AGENT_DESC\",
    \"owner\": \"Platform Team\",
    \"prompt\": \"$AGENT_PROMPT\",
    \"model_name\": \"claude-sonnet-4\",
    \"model_config_json\": {\"max_tokens\": 4096, \"temperature\": 0.2},
    \"supported_ides\": [\"cursor\", \"kiro\", \"claude-code\"],
    \"mcp_server_ids\": [\"$MCP_ID\"],
    \"goal_template\": {
      \"description\": \"Analyze input and produce structured output\",
      \"sections\": [
        {\"name\": \"Analysis\", \"grounding_required\": true},
        {\"name\": \"Recommendations\", \"grounding_required\": false}
      ]
    }
  }" "${AUTH[@]}")"
assert_status "T3.1 Create agent" 200 "$STATUS"
assert_json_field "T3.1 status" "$BODY" ".status" "active"
assert_json_nonempty "T3.1 id" "$BODY" ".id"
AGENT_ID=$(echo "$BODY" | jq -r '.id')

# T3.2 — Create Agent (Description Too Short)
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{
    \"name\": \"bad-agent\",
    \"version\": \"1.0.0\",
    \"description\": \"Too short\",
    \"owner\": \"Team\",
    \"prompt\": \"$AGENT_PROMPT\",
    \"model_name\": \"claude-sonnet-4\",
    \"goal_template\": {\"description\": \"Test\", \"sections\": [{\"name\": \"Output\"}]}
  }" "${AUTH[@]}")"
assert_status "T3.2 Short desc" 422 "$STATUS"

# T3.3 — Create Agent (No Goal Sections)
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{
    \"name\": \"bad-agent\",
    \"version\": \"1.0.0\",
    \"description\": \"$AGENT_DESC\",
    \"owner\": \"Team\",
    \"prompt\": \"$AGENT_PROMPT\",
    \"model_name\": \"claude-sonnet-4\",
    \"goal_template\": {\"description\": \"Test\", \"sections\": []}
  }" "${AUTH[@]}")"
assert_status "T3.3 No sections" 422 "$STATUS"

# T3.4 — Create Agent (Invalid MCP Reference)
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{
    \"name\": \"bad-agent\",
    \"version\": \"1.0.0\",
    \"description\": \"$AGENT_DESC\",
    \"owner\": \"Team\",
    \"prompt\": \"$AGENT_PROMPT\",
    \"model_name\": \"claude-sonnet-4\",
    \"mcp_server_ids\": [\"00000000-0000-0000-0000-000000000000\"],
    \"goal_template\": {\"description\": \"Test\", \"sections\": [{\"name\": \"Output\"}]}
  }" "${AUTH[@]}")"
assert_status "T3.4 Invalid MCP ref" 400 "$STATUS"

# T3.5 — List Agents
parse_response "$(curl_get "$BASE_URL/api/v1/agents")"
assert_status "T3.5 List agents" 200 "$STATUS"

# T3.6 — List Agents with Search
parse_response "$(curl_get "$BASE_URL/api/v1/agents?search=test")"
assert_status "T3.6 Search agents" 200 "$STATUS"

# T3.7 — Show Agent Detail
parse_response "$(curl_get "$BASE_URL/api/v1/agents/$AGENT_ID")"
assert_status "T3.7 Show agent" 200 "$STATUS"
assert_json_field "T3.7 id" "$BODY" ".id" "$AGENT_ID"
assert_json_nonempty "T3.7 goal_template" "$BODY" ".goal_template.description"

# T3.8 — Update Agent
parse_response "$(curl_put "$BASE_URL/api/v1/agents/$AGENT_ID" \
  '{"version": "1.1.0"}' "${AUTH[@]}")"
assert_status "T3.8 Update agent" 200 "$STATUS"
assert_json_field "T3.8 version" "$BODY" ".version" "1.1.0"

# T3.9 — Install Agent (Cursor)
parse_response "$(curl_post "$BASE_URL/api/v1/agents/$AGENT_ID/install" \
  '{"ide": "cursor"}' "${AUTH[@]}")"
assert_status "T3.9 Install cursor" 200 "$STATUS"
assert_json_nonempty "T3.9 rules_file" "$BODY" ".config_snippet.rules_file.path"

# T3.10 — Install Agent (Kiro)
parse_response "$(curl_post "$BASE_URL/api/v1/agents/$AGENT_ID/install" \
  '{"ide": "kiro"}' "${AUTH[@]}")"
assert_status "T3.10 Install kiro" 200 "$STATUS"
assert_json_nonempty "T3.10 rules_file" "$BODY" ".config_snippet.rules_file.path"

# T3.11 — Install Agent (Claude Code)
parse_response "$(curl_post "$BASE_URL/api/v1/agents/$AGENT_ID/install" \
  '{"ide": "claude-code"}' "${AUTH[@]}")"
assert_status "T3.11 Install claude-code" 200 "$STATUS"
assert_json_nonempty "T3.11 rules_file" "$BODY" ".config_snippet.rules_file.path"

# T3.12 — Install Agent (Gemini CLI)
parse_response "$(curl_post "$BASE_URL/api/v1/agents/$AGENT_ID/install" \
  '{"ide": "gemini-cli"}' "${AUTH[@]}")"
assert_status "T3.12 Install gemini-cli" 200 "$STATUS"
assert_json_nonempty "T3.12 rules_file" "$BODY" ".config_snippet.rules_file.path"

# T3.13 — Create Agent with No MCP Servers
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{
    \"name\": \"standalone-agent\",
    \"version\": \"1.0.0\",
    \"description\": \"$AGENT_DESC\",
    \"owner\": \"Platform Team\",
    \"prompt\": \"$AGENT_PROMPT\",
    \"model_name\": \"claude-sonnet-4\",
    \"mcp_server_ids\": [],
    \"goal_template\": {
      \"description\": \"Standalone analysis\",
      \"sections\": [{\"name\": \"Output\"}]
    }
  }" "${AUTH[@]}")"
assert_status "T3.13 Agent no MCPs" 200 "$STATUS"

# ══════════════════════════════════════════════════════════════════════════════
bold ""
bold "═══ Phase 4: Telemetry Tests ═══"
# ══════════════════════════════════════════════════════════════════════════════

# T4.1 — Telemetry Status (Empty/Baseline)
parse_response "$(curl_get "$BASE_URL/api/v1/telemetry/status" "${AUTH[@]}")"
assert_status "T4.1 Telemetry status" 200 "$STATUS"
assert_json_field "T4.1 status" "$BODY" ".status" "ok"

# T4.2 — Ingest Tool Call Event
parse_response "$(curl_post "$BASE_URL/api/v1/telemetry/events" \
  '{
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
  }' "${AUTH[@]}")"
assert_status "T4.2 Ingest tool call" 200 "$STATUS"
assert_json_field "T4.2 ingested" "$BODY" ".ingested" "1"

# T4.3 — Ingest Agent Interaction Event
parse_response "$(curl_post "$BASE_URL/api/v1/telemetry/events" \
  '{
    "agent_interactions": [{
      "agent_id": "test-agent",
      "session_id": "sess-001",
      "tool_calls": 3,
      "user_action": "accepted",
      "latency_ms": 1500,
      "ide": "cursor"
    }]
  }' "${AUTH[@]}")"
assert_status "T4.3 Ingest agent interaction" 200 "$STATUS"
assert_json_field "T4.3 ingested" "$BODY" ".ingested" "1"

# T4.4 — Ingest Batch (Mixed)
parse_response "$(curl_post "$BASE_URL/api/v1/telemetry/events" \
  '{
    "tool_calls": [
      {"mcp_server_id": "mcp-a", "tool_name": "tool1", "status": "success", "latency_ms": 100, "ide": "kiro"},
      {"mcp_server_id": "mcp-b", "tool_name": "tool2", "status": "error", "latency_ms": 500, "ide": "kiro"}
    ],
    "agent_interactions": [
      {"agent_id": "agent-x", "tool_calls": 5, "user_action": "rejected", "latency_ms": 2000, "ide": "kiro"}
    ]
  }' "${AUTH[@]}")"
assert_status "T4.4 Batch ingest" 200 "$STATUS"
assert_json_field "T4.4 ingested" "$BODY" ".ingested" "3"

# T4.5 — Telemetry Status (After Ingestion) — give ClickHouse a moment
sleep 2
parse_response "$(curl_get "$BASE_URL/api/v1/telemetry/status" "${AUTH[@]}")"
assert_status "T4.5 Status after ingest" 200 "$STATUS"
assert_json_gt "T4.5 tool events" "$BODY" ".tool_call_events" 0
assert_json_gt "T4.5 agent events" "$BODY" ".agent_interaction_events" 0

# T4.6 — Ingest Without Auth
parse_response "$(curl_post "$BASE_URL/api/v1/telemetry/events" \
  '{"tool_calls": [{"mcp_server_id": "x", "tool_name": "y"}]}')"
assert_status_oneof "T4.6 No auth" "$STATUS" 401 422

# T4.7 — Empty Batch
parse_response "$(curl_post "$BASE_URL/api/v1/telemetry/events" \
  '{"tool_calls": [], "agent_interactions": []}' "${AUTH[@]}")"
assert_status "T4.7 Empty batch" 200 "$STATUS"
assert_json_field "T4.7 ingested" "$BODY" ".ingested" "0"

# ══════════════════════════════════════════════════════════════════════════════
bold ""
bold "═══ Results ═══"
green "  Passed:  $PASS"
if [ "$FAIL" -gt 0 ]; then
  red "  Failed:  $FAIL"
  echo -e "  $FAILURES"
else
  green "  Failed:  0"
fi
if [ "$SKIP" -gt 0 ]; then
  yellow "  Skipped: $SKIP"
fi

bold ""
if [ "$FAIL" -gt 0 ]; then
  red "SOME TESTS FAILED"
  exit 1
else
  green "ALL TESTS PASSED ✓"
fi
