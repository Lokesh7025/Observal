#!/usr/bin/env bash
# Phase 5 & 6 — Idempotent Integration Test Script
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
DOCKER_DIR="${DOCKER_DIR:-$(cd "$(dirname "$0")/../docker" && pwd)}"
PASS=0
FAIL=0
SKIP=0
FAILURES=""

command -v jq >/dev/null 2>&1 || { echo "jq is required"; exit 1; }
command -v bc >/dev/null 2>&1 || { echo "bc is required"; exit 1; }

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
  local test_id="$1" actual="$2"; shift 2
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

assert_json_gte() {
  local test_id="$1" body="$2" field="$3" threshold="$4"
  local actual
  actual=$(echo "$body" | jq -r "$field" 2>/dev/null || echo "0")
  if [ "$(echo "$actual >= $threshold" | bc -l 2>/dev/null || echo 0)" -eq 1 ]; then
    green "  ✓ $test_id — $field = $actual (>= $threshold)"
    PASS=$((PASS + 1))
  else
    red "  ✗ $test_id — $field = $actual (expected >= $threshold)"
    FAIL=$((FAIL + 1))
    FAILURES="$FAILURES\n  ✗ $test_id"
  fi
}

assert_json_gt() {
  local test_id="$1" body="$2" field="$3" threshold="$4"
  local actual
  actual=$(echo "$body" | jq -r "$field" 2>/dev/null || echo "0")
  if [ "$(echo "$actual > $threshold" | bc -l 2>/dev/null || echo 0)" -eq 1 ]; then
    green "  ✓ $test_id — $field = $actual (> $threshold)"
    PASS=$((PASS + 1))
  else
    red "  ✗ $test_id — $field = $actual (expected > $threshold)"
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

curl_get() { local url="$1"; shift; curl -s -w "\n%{http_code}" "$url" "$@"; }
curl_post() { local url="$1" data="$2"; shift 2; curl -s -w "\n%{http_code}" -X POST "$url" -H "Content-Type: application/json" -d "$data" "$@"; }

parse_response() {
  local raw="$1"
  STATUS=$(echo "$raw" | tail -1)
  BODY=$(echo "$raw" | sed '$d')
}

wait_for_server() {
  bold "  ⏳ Waiting for server at $BASE_URL ..."
  for i in $(seq 1 30); do
    if curl -sf "$BASE_URL/health" > /dev/null 2>&1; then green "  Server is up!"; return; fi
    if [ "$i" -eq 30 ]; then red "  Server not reachable. Aborting."; exit 1; fi
    sleep 1
  done
}

# ── Bootstrap ────────────────────────────────────────────────────────────────

bold "═══ Bootstrap: Reset & Setup ═══"
yellow "  ↻ Resetting stack..."
docker compose -f "$DOCKER_DIR/docker-compose.yml" down -v > /dev/null 2>&1
docker compose -f "$DOCKER_DIR/docker-compose.yml" up -d > /dev/null 2>&1
wait_for_server

# Init admin
parse_response "$(curl_post "$BASE_URL/api/v1/auth/init" '{"email":"testadmin@observal.dev","name":"Test Admin"}')"
API_KEY=$(echo "$BODY" | jq -r '.api_key')
if [ -z "$API_KEY" ] || [ "$API_KEY" = "null" ]; then red "  Failed to init admin."; exit 1; fi
green "  Admin initialized."
AUTH=(-H "X-API-Key: $API_KEY")

LONG_DESC="This is a comprehensive test MCP server for integration testing purposes. It provides various utility tools and demonstrates the full lifecycle of MCP registration and validation."
AGENT_DESC="This is a comprehensive test agent for integration testing purposes. It analyzes input data and produces structured output with multiple sections for validation."
AGENT_PROMPT="You are a test agent for integration testing. Analyze the input provided and produce structured output with clear sections. Always cite sources and provide actionable recommendations."

# Submit + approve MCP
parse_response "$(curl_post "$BASE_URL/api/v1/mcps/submit" \
  "{\"git_url\":\"https://github.com/example/test-mcp.git\",\"name\":\"metrics-mcp\",\"version\":\"1.0.0\",\"description\":\"$LONG_DESC\",\"category\":\"utilities\",\"owner\":\"Platform Team\",\"supported_ides\":[\"cursor\"]}" "${AUTH[@]}")"
MCP_ID=$(echo "$BODY" | jq -r '.id')
curl_post "$BASE_URL/api/v1/review/$MCP_ID/approve" '{}' "${AUTH[@]}" > /dev/null
green "  MCP $MCP_ID approved."

# Install MCP (creates download record)
curl_post "$BASE_URL/api/v1/mcps/$MCP_ID/install" '{"ide":"cursor"}' "${AUTH[@]}" > /dev/null

# Create agent
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{\"name\":\"metrics-agent\",\"version\":\"1.0.0\",\"description\":\"$AGENT_DESC\",\"owner\":\"Platform Team\",\"prompt\":\"$AGENT_PROMPT\",\"model_name\":\"claude-sonnet-4\",\"supported_ides\":[\"cursor\"],\"mcp_server_ids\":[\"$MCP_ID\"],\"goal_template\":{\"description\":\"Test\",\"sections\":[{\"name\":\"Output\"}]}}" "${AUTH[@]}")"
AGENT_ID=$(echo "$BODY" | jq -r '.id')
green "  Agent $AGENT_ID created."

# Install agent (creates download record)
curl_post "$BASE_URL/api/v1/agents/$AGENT_ID/install" '{"ide":"cursor"}' "${AUTH[@]}" > /dev/null

# Send telemetry for the MCP
curl_post "$BASE_URL/api/v1/telemetry/events" \
  "{\"tool_calls\":[{\"mcp_server_id\":\"$MCP_ID\",\"tool_name\":\"test_tool\",\"status\":\"success\",\"latency_ms\":150,\"ide\":\"cursor\"},{\"mcp_server_id\":\"$MCP_ID\",\"tool_name\":\"test_tool\",\"status\":\"error\",\"latency_ms\":500,\"ide\":\"cursor\"}]}" "${AUTH[@]}" > /dev/null

# Send telemetry for the agent
curl_post "$BASE_URL/api/v1/telemetry/events" \
  "{\"agent_interactions\":[{\"agent_id\":\"$AGENT_ID\",\"tool_calls\":3,\"user_action\":\"accepted\",\"latency_ms\":1200,\"ide\":\"cursor\"},{\"agent_id\":\"$AGENT_ID\",\"tool_calls\":2,\"user_action\":\"rejected\",\"latency_ms\":800,\"ide\":\"cursor\"}]}" "${AUTH[@]}" > /dev/null

sleep 3  # Let ClickHouse flush
green "  Telemetry seeded."

# ══════════════════════════════════════════════════════════════════════════════
bold ""
bold "═══ Phase 5: Dashboard / Metrics Tests ═══"
# ══════════════════════════════════════════════════════════════════════════════

# T5.1 — MCP Metrics
parse_response "$(curl_get "$BASE_URL/api/v1/mcps/$MCP_ID/metrics" "${AUTH[@]}")"
assert_status "T5.1 MCP metrics" 200 "$STATUS"
assert_json_gte "T5.1 downloads" "$BODY" ".total_downloads" 1
assert_json_gte "T5.1 calls" "$BODY" ".total_calls" 2
assert_json_gt "T5.1 error_count" "$BODY" ".error_count" 0

# T5.2 — Agent Metrics
parse_response "$(curl_get "$BASE_URL/api/v1/agents/$AGENT_ID/metrics" "${AUTH[@]}")"
assert_status "T5.2 Agent metrics" 200 "$STATUS"
assert_json_gte "T5.2 downloads" "$BODY" ".total_downloads" 1
assert_json_gte "T5.2 interactions" "$BODY" ".total_interactions" 2

# T5.3 — Overview Stats
parse_response "$(curl_get "$BASE_URL/api/v1/overview/stats" "${AUTH[@]}")"
assert_status "T5.3 Overview stats" 200 "$STATUS"
assert_json_gte "T5.3 mcps" "$BODY" ".total_mcps" 1
assert_json_gte "T5.3 agents" "$BODY" ".total_agents" 1
assert_json_gte "T5.3 users" "$BODY" ".total_users" 1
assert_json_gte "T5.3 tool calls" "$BODY" ".total_tool_calls_today" 2

# T5.4 — Top MCPs
parse_response "$(curl_get "$BASE_URL/api/v1/overview/top-mcps" "${AUTH[@]}")"
assert_status "T5.4 Top MCPs" 200 "$STATUS"

# T5.5 — Top Agents
parse_response "$(curl_get "$BASE_URL/api/v1/overview/top-agents" "${AUTH[@]}")"
assert_status "T5.5 Top Agents" 200 "$STATUS"

# T5.6 — Trends
parse_response "$(curl_get "$BASE_URL/api/v1/overview/trends" "${AUTH[@]}")"
assert_status "T5.6 Trends" 200 "$STATUS"

# ══════════════════════════════════════════════════════════════════════════════
bold ""
bold "═══ Phase 6: Feedback Tests ═══"
# ══════════════════════════════════════════════════════════════════════════════

# T6.1 — Submit Feedback (MCP)
parse_response "$(curl_post "$BASE_URL/api/v1/feedback" \
  "{\"listing_id\":\"$MCP_ID\",\"listing_type\":\"mcp\",\"rating\":4,\"comment\":\"Great tool\"}" "${AUTH[@]}")"
assert_status "T6.1 Feedback MCP" 200 "$STATUS"
assert_json_field "T6.1 rating" "$BODY" ".rating" "4"

# T6.2 — Submit Feedback (Agent)
parse_response "$(curl_post "$BASE_URL/api/v1/feedback" \
  "{\"listing_id\":\"$AGENT_ID\",\"listing_type\":\"agent\",\"rating\":5,\"comment\":\"Excellent\"}" "${AUTH[@]}")"
assert_status "T6.2 Feedback Agent" 200 "$STATUS"
assert_json_field "T6.2 rating" "$BODY" ".rating" "5"

# T6.3 — Submit Feedback (Invalid Rating)
parse_response "$(curl_post "$BASE_URL/api/v1/feedback" \
  "{\"listing_id\":\"$MCP_ID\",\"listing_type\":\"mcp\",\"rating\":6}" "${AUTH[@]}")"
assert_status "T6.3 Invalid rating" 422 "$STATUS"

# T6.4 — Submit Feedback (Invalid Type)
parse_response "$(curl_post "$BASE_URL/api/v1/feedback" \
  "{\"listing_id\":\"$MCP_ID\",\"listing_type\":\"invalid\",\"rating\":3}" "${AUTH[@]}")"
assert_status "T6.4 Invalid type" 422 "$STATUS"

# T6.5 — Get MCP Feedback
parse_response "$(curl_get "$BASE_URL/api/v1/feedback/mcp/$MCP_ID")"
assert_status "T6.5 Get MCP feedback" 200 "$STATUS"

# T6.6 — Get Agent Feedback
parse_response "$(curl_get "$BASE_URL/api/v1/feedback/agent/$AGENT_ID")"
assert_status "T6.6 Get Agent feedback" 200 "$STATUS"

# T6.7 — Feedback Summary
parse_response "$(curl_get "$BASE_URL/api/v1/feedback/summary/$MCP_ID")"
assert_status "T6.7 Summary" 200 "$STATUS"
assert_json_field "T6.7 avg" "$BODY" ".average_rating" "4.0"
assert_json_field "T6.7 total" "$BODY" ".total_reviews" "1"

# T6.8 — My Feedback Received
parse_response "$(curl_get "$BASE_URL/api/v1/feedback/me" "${AUTH[@]}")"
assert_status "T6.8 My feedback" 200 "$STATUS"

# T6.9 — Submit Feedback Without Auth
parse_response "$(curl_post "$BASE_URL/api/v1/feedback" \
  "{\"listing_id\":\"$MCP_ID\",\"listing_type\":\"mcp\",\"rating\":3}")"
assert_status_oneof "T6.9 No auth" "$STATUS" 401 422

# T6.10 — Submit second MCP feedback + verify summary updates
parse_response "$(curl_post "$BASE_URL/api/v1/feedback" \
  "{\"listing_id\":\"$MCP_ID\",\"listing_type\":\"mcp\",\"rating\":2}" "${AUTH[@]}")"
assert_status "T6.10 Second feedback" 200 "$STATUS"

parse_response "$(curl_get "$BASE_URL/api/v1/feedback/summary/$MCP_ID")"
assert_json_field "T6.10 total" "$BODY" ".total_reviews" "2"
assert_json_field "T6.10 avg" "$BODY" ".average_rating" "3.0"

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
if [ "$SKIP" -gt 0 ]; then yellow "  Skipped: $SKIP"; fi

bold ""
if [ "$FAIL" -gt 0 ]; then red "SOME TESTS FAILED"; exit 1; else green "ALL TESTS PASSED ✓"; fi
