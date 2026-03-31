# Observal — Manual Setup & Walkthrough Guide

This guide walks through the complete Observal workflow from the perspective of three personas:
1. **Enterprise Admin** — sets up the server
2. **Developer** — submits MCP servers and creates agents
3. **User** — browses the registry and installs agents/MCPs into their IDE

---

## Part 1: Enterprise Admin — Server Setup

### 1.1 Prerequisites

- Docker & Docker Compose installed
- Git installed
- Python 3.11+ with `uv` (for the CLI)

### 1.2 Clone & Configure

```bash
git clone <your-observal-repo>
cd Observal

# Copy and edit environment config
cp .env.example .env
```

Edit `.env` — the defaults work for local dev:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@observal-db:5432/observal
CLICKHOUSE_URL=clickhouse://default:clickhouse@observal-clickhouse:8123/observal
SECRET_KEY=change-me-in-production
POSTGRES_USER=postgres
POSTGRES_PASSWORD=changeme
```

### 1.3 Start the Stack

```bash
cd docker
docker compose up -d
```

Wait for all services to be healthy:

```bash
docker compose ps
# All 3 services should be "running"
```

Verify the API is up:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 1.4 Install the CLI

```bash
cd ..  # back to project root
uv venv && source .venv/bin/activate
uv pip install -e .
```

### 1.5 Initialize the Admin Account

```bash
observal init
# Server URL: http://localhost:8000
# Admin email: admin@yourcompany.com
# Admin name: Admin
```

This creates the first admin user and saves the API key to `~/.observal/config.json`.

Verify:

```bash
observal whoami
# Admin (admin@yourcompany.com)
# Role: admin
```

### 1.6 Configure Enterprise Settings (Optional)

```bash
# Set feedback to be publicly visible
observal admin set feedback_visibility public

# View all settings
observal admin settings
```

### 1.7 Set Up the Eval Engine (SLM-as-a-Judge)

The eval engine scores agent performance. You have three options:

#### Option A: AWS Bedrock (Recommended for your setup)

Add to your `.env`:

```env
EVAL_MODEL_PROVIDER=bedrock
EVAL_MODEL_NAME=us.anthropic.claude-3-5-haiku-20241022-v1:0
AWS_REGION=ap-southeast-2
```

Make sure your AWS credentials are available to the container. Add to `docker-compose.yml` under `observal-api`:

```yaml
  observal-api:
    environment:
      - AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
      - AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
      - AWS_REGION=${AWS_REGION:-ap-southeast-2}
```

Then add your credentials to `.env`:

```env
AWS_ACCESS_KEY_ID=<your-key>
AWS_SECRET_ACCESS_KEY=<your-secret>
AWS_REGION=ap-southeast-2
```

Restart the API:

```bash
cd docker && docker compose up -d --build
```

#### Option B: OpenAI-Compatible API (OpenAI, Ollama, etc.)

```env
EVAL_MODEL_URL=https://api.openai.com/v1
EVAL_MODEL_API_KEY=sk-...
EVAL_MODEL_NAME=gpt-4o-mini
```

#### Option C: No LLM (Heuristic Fallback)

If you leave `EVAL_MODEL_NAME` empty, the eval engine uses a heuristic scorer based on:
- User acceptance rate (accepted/rejected)
- Response latency
- Tool call count

This is useful for testing the pipeline without LLM costs.

---

## Part 2: Developer — Submit an MCP Server

You are a developer who built a FastMCP server and wants to publish it to the enterprise registry.

### 2.1 Login

If you're not the admin, get an API key from your admin and login:

```bash
observal login
# Server URL: http://localhost:8000
# API Key: <your-key>
```

### 2.2 Submit Your MCP Server

```bash
observal submit https://github.com/your-org/your-fastmcp-server.git
```

Observal will:
1. Clone the repo
2. Scan for a FastMCP server definition
3. Extract tools, descriptions, and schemas
4. Pre-fill metadata from the repo

You'll be prompted to confirm/edit:

```
? Name: your-mcp-server
? Version (semver): 1.0.0
? Category: utilities
? Description: A comprehensive MCP server that provides...
? Owner / Team: Platform Team
? Supported IDEs (comma-separated): cursor, kiro, claude-code, gemini-cli
? Setup instructions: pip install your-mcp-server
? Changelog: Initial release
```

Output:

```
Submitted! ID: a1b2c3d4-... — Status: pending
```

### 2.3 Wait for Admin Approval

Your submission is now in the review queue. The admin will:

```bash
# Admin sees pending submissions
observal review list

# Admin reviews details
observal review show a1b2c3d4-...

# Admin approves
observal review approve a1b2c3d4-...
```

Or via curl:

```bash
# List pending
curl http://localhost:8000/api/v1/review \
  -H "X-API-Key: <admin_key>"

# Approve
curl -X POST http://localhost:8000/api/v1/review/a1b2c3d4-.../approve \
  -H "X-API-Key: <admin_key>"
```

### 2.4 Check Your MCP in the Registry

```bash
observal list
# Shows a table with your approved MCP server

observal show a1b2c3d4-...
# Full details: name, version, description, supported IDEs, git URL
```

---

## Part 3: Developer — Create an Agent

Agents are configuration objects that bundle a system prompt + MCP servers + model config + a goal template.

### 3.1 Create an Agent Interactively

```bash
observal agent create
```

You'll be prompted:

```
? Agent name: Incident Analyzer
? Version: 1.0.0
? Description (min 100 chars): An agent that analyzes support incidents by querying
  JIRA, searching knowledge bases, and producing structured root cause analysis
  with recommended next steps and component triage assignments.
? Owner / Team: Platform Team
? System prompt (min 50 chars): You are an incident analysis agent. When given an
  incident ID, analyze the support case and produce: 1. Root cause analysis
  2. Similar past incidents 3. Recommended next steps 4. Component triage.
  Always cite your sources.
? Model name: claude-sonnet-4
? Max tokens: 4096
? Temperature: 0.2
? Supported IDEs (comma-separated): cursor, kiro, claude-code
```

Then select MCP servers from the registry:

```
Available MCP Servers:
  ID          Name
  a1b2c3d4    your-mcp-server
  e5f6g7h8    jira-mcp

? MCP server IDs (comma-separated): a1b2c3d4, e5f6g7h8
```

Define the goal template (what the agent should produce):

```
? Goal template description: Analyze incident and produce structured report
? Goal section name (or 'done'): Root Cause
?   Description: Identify the root cause of the incident
?   Grounding required? Yes
? Goal section name (or 'done'): Similar Incidents
?   Grounding required? Yes
? Goal section name (or 'done'): Next Steps
?   Grounding required? No
? Goal section name (or 'done'): done
```

Output:

```
Agent created! ID: x1y2z3w4-...
```

### 3.2 Or Create via API

```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your_key>" \
  -d '{
    "name": "Incident Analyzer",
    "version": "1.0.0",
    "description": "An agent that analyzes support incidents by querying JIRA, searching knowledge bases, and producing structured root cause analysis with recommended next steps and component triage assignments.",
    "owner": "Platform Team",
    "prompt": "You are an incident analysis agent. When given an incident ID, analyze the support case and produce: 1. Root cause analysis 2. Similar past incidents 3. Recommended next steps 4. Component triage. Always cite your sources.",
    "model_name": "claude-sonnet-4",
    "model_config_json": {"max_tokens": 4096, "temperature": 0.2},
    "supported_ides": ["cursor", "kiro", "claude-code"],
    "mcp_server_ids": ["<mcp-id-1>", "<mcp-id-2>"],
    "goal_template": {
      "description": "Analyze incident and produce structured report",
      "sections": [
        {"name": "Root Cause", "grounding_required": true},
        {"name": "Similar Incidents", "grounding_required": true},
        {"name": "Next Steps", "grounding_required": false}
      ]
    }
  }'
```

---

## Part 4: User — Browse & Install

You are a developer who wants to use MCP servers and agents in your IDE.

### 4.1 Browse the Registry

```bash
# List all approved MCP servers
observal list

# Search
observal list --search "jira"

# Filter by category
observal list --category utilities

# List all agents
observal agent list

# Search agents
observal agent list --search "incident"
```

### 4.2 View Details

```bash
# MCP server details
observal show <mcp-id>

# Agent details (shows linked MCPs, goal template, model config)
observal agent show <agent-id>
```

### 4.3 Install an MCP Server

```bash
observal install <mcp-id> --ide kiro
```

This outputs a config snippet you paste into your IDE config. For Kiro:

```json
{
  "mcpServers": {
    "your-mcp-server": {
      "command": "python",
      "args": ["-m", "your-mcp-server"],
      "env": {}
    }
  }
}
```

Paste this into `.kiro/mcp.json` in your project.

### 4.4 Install an Agent to Kiro

```bash
observal agent install <agent-id> --ide kiro
```

This generates a complete setup:

```json
{
  "rules_file": {
    "path": ".kiro/rules/incident-analyzer.md",
    "content": "You are an incident analysis agent..."
  },
  "mcp_json": {
    "mcpServers": {
      "your-mcp-server": {
        "command": "python",
        "args": ["-m", "your-mcp-server"],
        "env": {}
      },
      "jira-mcp": {
        "command": "python",
        "args": ["-m", "jira-mcp"],
        "env": {}
      }
    }
  }
}
```

To set up manually:

1. Create `.kiro/rules/incident-analyzer.md` with the prompt content
2. Merge the `mcpServers` into your `.kiro/mcp.json`
3. Install the MCP server dependencies (e.g., `pip install your-mcp-server jira-mcp`)

### 4.5 Install for Other IDEs

```bash
# Cursor
observal agent install <agent-id> --ide cursor
# Creates .rules/agent-name.md + mcpServers config

# Claude Code
observal agent install <agent-id> --ide claude-code
# Creates .claude/rules/agent-name.md + claude mcp add commands

# Gemini CLI
observal agent install <agent-id> --ide gemini-cli
# Creates GEMINI.md + mcpServers config
```

---

## Part 5: Telemetry — Observing Usage

Once MCP servers and agents are installed with Observal hooks, telemetry flows automatically.

### 5.1 Send Test Telemetry

```bash
observal telemetry test
# Test event sent! Ingested: 1

observal telemetry status
# Status: ok
# Tool call events (last hour): 1
# Agent interaction events (last hour): 0
```

### 5.2 Simulate Real Telemetry

```bash
# Simulate MCP tool calls
curl -X POST http://localhost:8000/api/v1/telemetry/events \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your_key>" \
  -d '{
    "tool_calls": [
      {"mcp_server_id": "<mcp-id>", "tool_name": "search_issues", "status": "success", "latency_ms": 340, "ide": "kiro"},
      {"mcp_server_id": "<mcp-id>", "tool_name": "get_issue", "status": "success", "latency_ms": 120, "ide": "kiro"}
    ],
    "agent_interactions": [
      {"agent_id": "<agent-id>", "tool_calls": 3, "user_action": "accepted", "latency_ms": 2500, "ide": "kiro"}
    ]
  }'
```

### 5.3 View Metrics

```bash
# MCP metrics
observal metrics <mcp-id> --type mcp
# Total downloads: 3
# Total calls: 15
# Error rate: 2.0%
# Avg latency: 230ms
# p50/p90/p99: 180/450/890ms

# Agent metrics
observal metrics <agent-id> --type agent
# Total interactions: 8
# Acceptance rate: 75.0%
# Avg tool calls: 3.2

# Enterprise overview
observal overview
# MCPs: 5, Agents: 3, Users: 12
# Tool calls today: 142
# Agent interactions today: 28
```

---

## Part 6: Eval Engine — Scoring Agent Performance

### 6.1 Run an Evaluation

After telemetry has been flowing for your agent:

```bash
observal eval run <agent-id>
```

Output:

```
Eval Run: e1f2g3h4-...
  Status: completed
  Traces evaluated: 8
  Scorecard abc12345... — Score: 7.2/10 (B)
  Scorecard def67890... — Score: 8.5/10 (A)
```

### 6.2 View Scorecards

```bash
# List all scorecards for an agent
observal eval scorecards <agent-id>

# Filter by version
observal eval scorecards <agent-id> --version 1.0.0

# View a specific scorecard
observal eval show <scorecard-id>
```

Scorecard detail:

```
Scorecard abc12345-...
  Overall: 7.2/10 (B)
  Bottleneck: tool_usage_efficiency
  Recommendations: Consider caching JIRA responses to reduce redundant API calls.
  Dimensions:
    task_completion: 8.0/10 (A) — Successfully produced all required sections
    tool_usage_efficiency: 5.5/10 (D) — Made 6 redundant tool calls
    response_quality: 7.0/10 (B) — Clear structure, some sections lack detail
    factual_grounding: 8.0/10 (A) — All claims verified against source data
    user_satisfaction: 7.5/10 (B) — Latency: 2500ms, user accepted
```

### 6.3 Compare Agent Versions

After updating your agent (new prompt, different MCPs), compare:

```bash
observal eval compare <agent-id> --a 1.0.0 --b 2.0.0
```

```
Version Comparison
  1.0.0: avg 6.8/10 (12 scorecards)
  2.0.0: avg 8.1/10 (8 scorecards)
```

### 6.4 How the Eval Engine Works

The eval engine:
1. Fetches recent agent interaction traces from ClickHouse
2. For each trace, assembles the agent's goal template + trace data
3. Sends it to the configured LLM (Bedrock/OpenAI) as a judge
4. The judge scores 5 dimensions (0-10 each) and identifies the bottleneck
5. Stores scorecards in PostgreSQL for historical tracking

**With Bedrock configured**, the judge uses Claude Haiku to evaluate traces — fast and cheap.

**Without an LLM**, it falls back to heuristic scoring based on acceptance rate, latency, and tool call patterns.

---

## Part 7: Feedback

### 7.1 Rate an MCP or Agent

```bash
# Rate an MCP server
observal rate <mcp-id> --stars 4 --comment "Great tool, fast responses"

# Rate an agent
observal rate <agent-id> --stars 5 --type agent --comment "Excellent analysis"
```

### 7.2 View Feedback

```bash
observal feedback <mcp-id>
# Average: 4.2/5 (7 reviews)
#   ★★★★★ — Excellent analysis
#   ★★★★☆ — Great tool, fast responses
#   ★★★☆☆ — Needs better error handling
```

---

## Quick Reference — All CLI Commands

| Command | Description |
|---|---|
| `observal init` | First-run setup (create admin) |
| `observal login` | Login with API key |
| `observal whoami` | Show current user |
| `observal submit <git_url>` | Submit MCP server for review |
| `observal list` | List approved MCP servers |
| `observal show <id>` | Show MCP details |
| `observal install <id> --ide <ide>` | Get MCP install config |
| `observal review list` | List pending reviews (admin) |
| `observal review approve <id>` | Approve submission (admin) |
| `observal review reject <id> -r "reason"` | Reject submission (admin) |
| `observal agent create` | Create agent interactively |
| `observal agent list` | List active agents |
| `observal agent show <id>` | Show agent details |
| `observal agent install <id> --ide <ide>` | Get agent install config |
| `observal telemetry status` | Check telemetry flow |
| `observal telemetry test` | Send test event |
| `observal metrics <id> --type mcp\|agent` | View metrics |
| `observal overview` | Enterprise overview stats |
| `observal eval run <agent-id>` | Run evaluation |
| `observal eval scorecards <agent-id>` | List scorecards |
| `observal eval show <scorecard-id>` | Scorecard detail |
| `observal eval compare <id> --a v1 --b v2` | Compare versions |
| `observal rate <id> --stars N` | Rate a listing |
| `observal feedback <id>` | View feedback |
| `observal admin settings` | List enterprise settings |
| `observal admin set <key> <value>` | Set enterprise setting |
| `observal admin users` | List users |

---

## API Quick Reference

All API endpoints require `X-API-Key` header unless noted.

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| GET | `/health` | No | Health check |
| POST | `/api/v1/auth/init` | No | Create admin (first run) |
| POST | `/api/v1/auth/login` | No | Login |
| GET | `/api/v1/auth/whoami` | Yes | Current user |
| POST | `/api/v1/mcps/analyze` | Yes | Analyze git repo |
| POST | `/api/v1/mcps/submit` | Yes | Submit MCP |
| GET | `/api/v1/mcps` | No | List approved MCPs |
| GET | `/api/v1/mcps/{id}` | No | MCP detail |
| POST | `/api/v1/mcps/{id}/install` | Yes | Get install config |
| GET | `/api/v1/mcps/{id}/metrics` | Yes | MCP metrics |
| GET | `/api/v1/review` | Admin | List pending |
| POST | `/api/v1/review/{id}/approve` | Admin | Approve |
| POST | `/api/v1/review/{id}/reject` | Admin | Reject |
| POST | `/api/v1/agents` | Yes | Create agent |
| GET | `/api/v1/agents` | No | List agents |
| GET | `/api/v1/agents/{id}` | No | Agent detail |
| PUT | `/api/v1/agents/{id}` | Yes | Update agent |
| POST | `/api/v1/agents/{id}/install` | Yes | Get install config |
| GET | `/api/v1/agents/{id}/metrics` | Yes | Agent metrics |
| POST | `/api/v1/telemetry/events` | Yes | Ingest telemetry |
| GET | `/api/v1/telemetry/status` | Yes | Telemetry status |
| GET | `/api/v1/overview/stats` | Yes | Enterprise stats |
| GET | `/api/v1/overview/top-mcps` | Yes | Top MCPs |
| GET | `/api/v1/overview/top-agents` | Yes | Top agents |
| GET | `/api/v1/overview/trends` | Yes | 30-day trends |
| POST | `/api/v1/eval/agents/{id}` | Yes | Run evaluation |
| GET | `/api/v1/eval/agents/{id}/runs` | Yes | List eval runs |
| GET | `/api/v1/eval/agents/{id}/scorecards` | Yes | List scorecards |
| GET | `/api/v1/eval/scorecards/{id}` | Yes | Scorecard detail |
| GET | `/api/v1/eval/agents/{id}/compare` | Yes | Compare versions |
| POST | `/api/v1/feedback` | Yes | Submit feedback |
| GET | `/api/v1/feedback/mcp/{id}` | No | MCP feedback |
| GET | `/api/v1/feedback/agent/{id}` | No | Agent feedback |
| GET | `/api/v1/feedback/summary/{id}` | No | Feedback summary |
| GET | `/api/v1/feedback/me` | Yes | My received feedback |
| GET | `/api/v1/admin/settings` | Admin | List settings |
| PUT | `/api/v1/admin/settings/{key}` | Admin | Upsert setting |
| DELETE | `/api/v1/admin/settings/{key}` | Admin | Delete setting |
| GET | `/api/v1/admin/users` | Admin | List users |
| PUT | `/api/v1/admin/users/{id}/role` | Admin | Update role |
