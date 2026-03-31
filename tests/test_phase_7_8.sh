#!/usr/bin/env bash
# Phase 7 & 8 — Idempotent Integration Test Script
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
DOCKER_DIR="${DOCKER_DIR:-$(cd "$(dirname "$0")/../docker" && pwd)}"
PASS=0 FAIL=0 SKIP=0 FAILURES=""

command -v jq >/dev/null 2>&1 || { echo "jq is required"; exit 1; }

green()  { printf "\033[32m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }

assert_status() {
  local t="$1" e="$2" a="$3"
  if [ "$a" -eq "$e" ]; then green "  ✓ $t — HTTP $a"; PASS=$((PASS+1))
  else red "  ✗ $t — HTTP $a (expected $e)"; FAIL=$((FAIL+1)); FAILURES="$FAILURES\n  ✗ $t"; fi
}
assert_json_field() {
  local t="$1" b="$2" f="$3" e="$4"
  local a; a=$(echo "$b" | jq -r "$f" 2>/dev/null || echo "__err__")
  if [ "$a" = "$e" ]; then green "  ✓ $t — $f = $e"; PASS=$((PASS+1))
  else red "  ✗ $t — $f = '$a' (expected '$e')"; FAIL=$((FAIL+1)); FAILURES="$FAILURES\n  ✗ $t"; fi
}
assert_json_nonempty() {
  local t="$1" b="$2" f="$3"
  local a; a=$(echo "$b" | jq -r "$f" 2>/dev/null || echo "")
  if [ -n "$a" ] && [ "$a" != "null" ]; then green "  ✓ $t — $f present"; PASS=$((PASS+1))
  else red "  ✗ $t — $f empty/null"; FAIL=$((FAIL+1)); FAILURES="$FAILURES\n  ✗ $t"; fi
}
assert_json_gte() {
  local t="$1" b="$2" f="$3" th="$4"
  local a; a=$(echo "$b" | jq -r "$f" 2>/dev/null || echo "0")
  if [ "$(echo "$a >= $th" | bc -l 2>/dev/null || echo 0)" -eq 1 ]; then green "  ✓ $t — $f = $a (>= $th)"; PASS=$((PASS+1))
  else red "  ✗ $t — $f = $a (expected >= $th)"; FAIL=$((FAIL+1)); FAILURES="$FAILURES\n  ✗ $t"; fi
}

curl_get() { local u="$1"; shift; curl -s -w "\n%{http_code}" "$u" "$@"; }
curl_post() { local u="$1" d="$2"; shift 2; curl -s -w "\n%{http_code}" -X POST "$u" -H "Content-Type: application/json" -d "$d" "$@"; }
curl_put() { local u="$1" d="$2"; shift 2; curl -s -w "\n%{http_code}" -X PUT "$u" -H "Content-Type: application/json" -d "$d" "$@"; }
curl_delete() { local u="$1"; shift; curl -s -w "\n%{http_code}" -X DELETE "$u" "$@"; }

parse_response() { STATUS=$(echo "$1" | tail -1); BODY=$(echo "$1" | sed '$d'); }

wait_for_server() {
  bold "  ⏳ Waiting for server..."
  for i in $(seq 1 30); do
    curl -sf "$BASE_URL/health" > /dev/null 2>&1 && { green "  Server is up!"; return; }
    [ "$i" -eq 30 ] && { red "  Server not reachable."; exit 1; }; sleep 1
  done
}

# ── Bootstrap ────────────────────────────────────────────

bold "═══ Bootstrap ═══"
yellow "  ↻ Resetting stack..."
docker compose -f "$DOCKER_DIR/docker-compose.yml" down -v > /dev/null 2>&1
docker compose -f "$DOCKER_DIR/docker-compose.yml" up -d > /dev/null 2>&1
wait_for_server

parse_response "$(curl_post "$BASE_URL/api/v1/auth/init" '{"email":"testadmin@observal.dev","name":"Test Admin"}')"
API_KEY=$(echo "$BODY" | jq -r '.api_key')
USER_ID=$(echo "$BODY" | jq -r '.user.id')
[ -z "$API_KEY" ] || [ "$API_KEY" = "null" ] && { red "  Init failed."; exit 1; }
green "  Admin initialized."
AUTH=(-H "X-API-Key: $API_KEY")

LONG_DESC="This is a comprehensive test MCP server for integration testing purposes. It provides various utility tools and demonstrates the full lifecycle of MCP registration and validation."
AGENT_DESC="This is a comprehensive test agent for integration testing purposes. It analyzes input data and produces structured output with multiple sections for validation."
AGENT_PROMPT="You are a test agent for integration testing. Analyze the input provided and produce structured output with clear sections. Always cite sources and provide actionable recommendations."

# MCP
parse_response "$(curl_post "$BASE_URL/api/v1/mcps/submit" \
  "{\"git_url\":\"https://github.com/example/test.git\",\"name\":\"eval-mcp\",\"version\":\"1.0.0\",\"description\":\"$LONG_DESC\",\"category\":\"utilities\",\"owner\":\"Team\",\"supported_ides\":[\"cursor\"]}" "${AUTH[@]}")"
MCP_ID=$(echo "$BODY" | jq -r '.id')
curl_post "$BASE_URL/api/v1/review/$MCP_ID/approve" '{}' "${AUTH[@]}" > /dev/null
green "  MCP approved."

# Agent
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{\"name\":\"eval-agent\",\"version\":\"1.0.0\",\"description\":\"$AGENT_DESC\",\"owner\":\"Team\",\"prompt\":\"$AGENT_PROMPT\",\"model_name\":\"claude-sonnet-4\",\"supported_ides\":[\"cursor\"],\"mcp_server_ids\":[\"$MCP_ID\"],\"goal_template\":{\"description\":\"Analyze and produce output\",\"sections\":[{\"name\":\"Analysis\",\"grounding_required\":true},{\"name\":\"Recommendations\"}]}}" "${AUTH[@]}")"
AGENT_ID=$(echo "$BODY" | jq -r '.id')
green "  Agent $AGENT_ID created."

# Telemetry
curl_post "$BASE_URL/api/v1/telemetry/events" \
  "{\"agent_interactions\":[{\"agent_id\":\"$AGENT_ID\",\"tool_calls\":3,\"user_action\":\"accepted\",\"latency_ms\":1200,\"ide\":\"cursor\"},{\"agent_id\":\"$AGENT_ID\",\"tool_calls\":1,\"user_action\":\"rejected\",\"latency_ms\":5000,\"ide\":\"cursor\"}]}" "${AUTH[@]}" > /dev/null
sleep 3
green "  Telemetry seeded."

# Agent with no traces
parse_response "$(curl_post "$BASE_URL/api/v1/agents" \
  "{\"name\":\"empty-agent\",\"version\":\"1.0.0\",\"description\":\"$AGENT_DESC\",\"owner\":\"Team\",\"prompt\":\"$AGENT_PROMPT\",\"model_name\":\"claude-sonnet-4\",\"goal_template\":{\"description\":\"Test\",\"sections\":[{\"name\":\"Output\"}]}}" "${AUTH[@]}")"
EMPTY_AGENT_ID=$(echo "$BODY" | jq -r '.id')

# ══════════════════════════════════════════════════════════
bold ""
bold "═══ Phase 7: Eval Engine Tests ═══"
# ══════════════════════════════════════════════════════════

# T7.1 — Run Evaluation (With Traces)
parse_response "$(curl_post "$BASE_URL/api/v1/eval/agents/$AGENT_ID" '{}' "${AUTH[@]}")"
assert_status "T7.1 Eval run" 200 "$STATUS"
assert_json_field "T7.1 status" "$BODY" ".status" "completed"
assert_json_gte "T7.1 traces" "$BODY" ".traces_evaluated" 1
SCORECARD_ID=$(echo "$BODY" | jq -r '.scorecards[0].id // empty')

# T7.2 — Run Evaluation (No Traces)
parse_response "$(curl_post "$BASE_URL/api/v1/eval/agents/$EMPTY_AGENT_ID" '{}' "${AUTH[@]}")"
assert_status "T7.2 Eval no traces" 200 "$STATUS"
assert_json_field "T7.2 status" "$BODY" ".status" "completed"
assert_json_field "T7.2 traces" "$BODY" ".traces_evaluated" "0"

# T7.3 — List Eval Runs
parse_response "$(curl_get "$BASE_URL/api/v1/eval/agents/$AGENT_ID/runs" "${AUTH[@]}")"
assert_status "T7.3 List runs" 200 "$STATUS"

# T7.4 — List Scorecards
parse_response "$(curl_get "$BASE_URL/api/v1/eval/agents/$AGENT_ID/scorecards" "${AUTH[@]}")"
assert_status "T7.4 List scorecards" 200 "$STATUS"

# T7.5 — List Scorecards (Filter by Version)
parse_response "$(curl_get "$BASE_URL/api/v1/eval/agents/$AGENT_ID/scorecards?version=1.0.0" "${AUTH[@]}")"
assert_status "T7.5 Filter version" 200 "$STATUS"

# T7.6 — Get Scorecard Detail
if [ -n "$SCORECARD_ID" ]; then
  parse_response "$(curl_get "$BASE_URL/api/v1/eval/scorecards/$SCORECARD_ID" "${AUTH[@]}")"
  assert_status "T7.6 Scorecard detail" 200 "$STATUS"
  assert_json_nonempty "T7.6 overall_grade" "$BODY" ".overall_grade"
  # Check 5 dimensions
  DIM_COUNT=$(echo "$BODY" | jq '.dimensions | length' 2>/dev/null || echo 0)
  if [ "$DIM_COUNT" -eq 5 ]; then green "  ✓ T7.6 dimensions — 5 dimensions"; PASS=$((PASS+1))
  else red "  ✗ T7.6 dimensions — $DIM_COUNT (expected 5)"; FAIL=$((FAIL+1)); FAILURES="$FAILURES\n  ✗ T7.6 dimensions"; fi
else
  yellow "  ⊘ T7.6 Skipped — no scorecard ID"; SKIP=$((SKIP+1))
fi

# T7.7 — Compare Versions
parse_response "$(curl_get "$BASE_URL/api/v1/eval/agents/$AGENT_ID/compare?version_a=1.0.0&version_b=2.0.0" "${AUTH[@]}")"
assert_status "T7.7 Compare" 200 "$STATUS"
assert_json_nonempty "T7.7 version_a" "$BODY" ".version_a.version"

# T7.8 — Evaluate Non-Existent Agent
parse_response "$(curl_post "$BASE_URL/api/v1/eval/agents/00000000-0000-0000-0000-000000000000" '{}' "${AUTH[@]}")"
assert_status "T7.8 Not found" 404 "$STATUS"

# T7.9 — Get Non-Existent Scorecard
parse_response "$(curl_get "$BASE_URL/api/v1/eval/scorecards/00000000-0000-0000-0000-000000000000" "${AUTH[@]}")"
assert_status "T7.9 SC not found" 404 "$STATUS"

# ══════════════════════════════════════════════════════════
bold ""
bold "═══ Phase 8: Admin API Tests ═══"
# ══════════════════════════════════════════════════════════

# T8.1 — List Settings (Empty)
parse_response "$(curl_get "$BASE_URL/api/v1/admin/settings" "${AUTH[@]}")"
assert_status "T8.1 List settings" 200 "$STATUS"

# T8.2 — Create Setting
parse_response "$(curl_put "$BASE_URL/api/v1/admin/settings/feedback_visibility" '{"value":"public"}' "${AUTH[@]}")"
assert_status "T8.2 Create setting" 200 "$STATUS"
assert_json_field "T8.2 key" "$BODY" ".key" "feedback_visibility"
assert_json_field "T8.2 value" "$BODY" ".value" "public"

# T8.3 — Get Setting
parse_response "$(curl_get "$BASE_URL/api/v1/admin/settings/feedback_visibility" "${AUTH[@]}")"
assert_status "T8.3 Get setting" 200 "$STATUS"
assert_json_field "T8.3 value" "$BODY" ".value" "public"

# T8.4 — Update Setting
parse_response "$(curl_put "$BASE_URL/api/v1/admin/settings/feedback_visibility" '{"value":"private"}' "${AUTH[@]}")"
assert_status "T8.4 Update setting" 200 "$STATUS"
assert_json_field "T8.4 value" "$BODY" ".value" "private"

# T8.5 — List Settings (After Create)
parse_response "$(curl_get "$BASE_URL/api/v1/admin/settings" "${AUTH[@]}")"
assert_status "T8.5 List after create" 200 "$STATUS"

# T8.6 — Delete Setting
parse_response "$(curl_delete "$BASE_URL/api/v1/admin/settings/feedback_visibility" "${AUTH[@]}")"
assert_status "T8.6 Delete setting" 200 "$STATUS"

# T8.7 — Get Deleted Setting
parse_response "$(curl_get "$BASE_URL/api/v1/admin/settings/feedback_visibility" "${AUTH[@]}")"
assert_status "T8.7 Deleted 404" 404 "$STATUS"

# T8.8 — List Users
parse_response "$(curl_get "$BASE_URL/api/v1/admin/users" "${AUTH[@]}")"
assert_status "T8.8 List users" 200 "$STATUS"

# T8.9 — Update User Role (Invalid)
parse_response "$(curl_put "$BASE_URL/api/v1/admin/users/$USER_ID/role" '{"role":"superadmin"}' "${AUTH[@]}")"
assert_status "T8.9 Invalid role" 422 "$STATUS"

# T8.10 — Delete Non-Existent Setting
parse_response "$(curl_delete "$BASE_URL/api/v1/admin/settings/nonexistent" "${AUTH[@]}")"
assert_status "T8.10 Delete 404" 404 "$STATUS"

# T8.11 — Self-Demotion Blocked
parse_response "$(curl_put "$BASE_URL/api/v1/admin/users/$USER_ID/role" '{"role":"developer"}' "${AUTH[@]}")"
assert_status "T8.11 Self-demote blocked" 400 "$STATUS"

# ══════════════════════════════════════════════════════════
bold ""
bold "═══ Results ═══"
green "  Passed:  $PASS"
if [ "$FAIL" -gt 0 ]; then red "  Failed:  $FAIL"; echo -e "  $FAILURES"
else green "  Failed:  0"; fi
if [ "$SKIP" -gt 0 ]; then yellow "  Skipped: $SKIP"; fi
bold ""
if [ "$FAIL" -gt 0 ]; then red "SOME TESTS FAILED"; exit 1; else green "ALL TESTS PASSED ✓"; fi
