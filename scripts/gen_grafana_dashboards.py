# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Generate Grafana dashboards for the DuckDB telemetry store.

Each panel posts DuckDB SQL to the store's read-only ``/v1/query`` endpoint via
the Infinity datasource. Regenerate with::

    uv run --project observal-server python scripts/gen_grafana_dashboards.py

The generated JSON is committed under ``grafana/dashboards/``.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "grafana" / "dashboards"
DATASOURCE = {"type": "yesoreyeram-infinity-datasource", "uid": "observal-telemetry"}

TIME_PARAMS = {"from": "${__from:date:iso}", "to": "${__to:date:iso}"}


def target(sql: str, *, fmt: str = "table", columns: list[dict] | None = None, ref: str = "A") -> dict:
    body = {"sql": " ".join(sql.split()), "params": {**TIME_PARAMS, "bucket": "${bucket}"}}
    return {
        "refId": ref,
        "datasource": DATASOURCE,
        "type": "json",
        "source": "url",
        "format": fmt,
        "parser": "backend",
        "url": "${telemetry_url}/v1/query",
        "url_options": {
            "method": "POST",
            "body_type": "raw",
            "body_content_type": "application/json",
            "data": json.dumps(body),
        },
        "root_selector": "rows",
        "columns": columns or [],
    }


def col(name: str, kind: str = "string") -> dict:
    return {"selector": name, "text": name, "type": kind}


_id = 0


def panel(title: str, kind: str, targets: list[dict], *, x: int, y: int, w: int = 12, h: int = 8, **extra) -> dict:
    global _id
    _id += 1
    p = {
        "id": _id,
        "title": title,
        "type": kind,
        "datasource": DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    p.update(extra)
    return p


def dashboard(uid: str, title: str, panels: list[dict], *, description: str) -> dict:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["observal", "telemetry"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "refresh": "1m",
        "time": {"from": "now-7d", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "telemetry_url",
                    "label": "Telemetry store URL",
                    "type": "constant",
                    "query": "http://observal-telemetry:8125",
                    "current": {"text": "http://observal-telemetry:8125", "value": "http://observal-telemetry:8125"},
                    "hide": 2,
                },
                {
                    "name": "bucket",
                    "label": "Bucket",
                    "type": "custom",
                    "query": "1 hour,1 day,15 minutes,5 minutes",
                    "current": {"text": "1 hour", "value": "1 hour"},
                    "options": [
                        {"text": v, "value": v, "selected": v == "1 hour"}
                        for v in ("1 hour", "1 day", "15 minutes", "5 minutes")
                    ],
                },
            ]
        },
        "panels": panels,
    }


TS = [col("time", "timestamp")]
IN_RANGE = "last_event_time >= CAST($from AS TIMESTAMP) AND last_event_time < CAST($to AS TIMESTAMP)"
EV_RANGE = '"timestamp" >= CAST($from AS TIMESTAMP) AND "timestamp" < CAST($to AS TIMESTAMP)'
BUCKET = "time_bucket(CAST($bucket AS INTERVAL), last_event_time)"
EV_BUCKET = 'time_bucket(CAST($bucket AS INTERVAL), "timestamp")'


def session_overview() -> dict:
    global _id
    _id = 0
    panels = [
        panel(
            "Sessions",
            "stat",
            [target(f"SELECT count(*) AS sessions FROM session_stats_agg WHERE {IN_RANGE}")],
            x=0,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Active users",
            "stat",
            [
                target(
                    f"SELECT count(DISTINCT user_id) AS users FROM session_stats_agg WHERE user_id <> '' AND {IN_RANGE}"
                )
            ],
            x=6,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Prompts",
            "stat",
            [target(f"SELECT coalesce(sum(prompt_count), 0) AS prompts FROM session_stats_agg WHERE {IN_RANGE}")],
            x=12,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Tool calls",
            "stat",
            [target(f"SELECT coalesce(sum(tool_call_count), 0) AS tool_calls FROM session_stats_agg WHERE {IN_RANGE}")],
            x=18,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Sessions over time",
            "timeseries",
            [
                target(
                    f"SELECT {BUCKET} AS time, count(*) AS sessions FROM session_stats_agg WHERE {IN_RANGE} "
                    "GROUP BY time ORDER BY time",
                    fmt="timeseries",
                    columns=[*TS, col("sessions", "number")],
                )
            ],
            x=0,
            y=4,
            w=16,
        ),
        panel(
            "Sessions by harness",
            "piechart",
            [
                target(
                    f"SELECT harness, count(*) AS sessions FROM session_stats_agg WHERE harness <> '' AND {IN_RANGE} "
                    "GROUP BY harness ORDER BY sessions DESC"
                )
            ],
            x=16,
            y=4,
            w=8,
        ),
        panel(
            "Recent sessions",
            "table",
            [
                target(
                    "SELECT session_id, user_id, harness, agent_id, prompt_count, tool_call_count, "
                    f"input_tokens + output_tokens AS tokens, last_event_time FROM session_stats_agg WHERE {IN_RANGE} "
                    "ORDER BY last_event_time DESC LIMIT 50"
                )
            ],
            x=0,
            y=12,
            w=24,
            h=10,
        ),
    ]
    return dashboard(
        "observal-session-overview",
        "Observal / Session Overview",
        panels,
        description="Session volume, active users, and harness mix from the telemetry store.",
    )


def token_usage() -> dict:
    global _id
    _id = 0
    panels = [
        panel(
            "Input tokens",
            "stat",
            [target(f"SELECT coalesce(sum(input_tokens), 0) AS input_tokens FROM session_stats_agg WHERE {IN_RANGE}")],
            x=0,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Output tokens",
            "stat",
            [
                target(
                    f"SELECT coalesce(sum(output_tokens), 0) AS output_tokens FROM session_stats_agg WHERE {IN_RANGE}"
                )
            ],
            x=6,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Cache read tokens",
            "stat",
            [
                target(
                    f"SELECT coalesce(sum(cache_read_tokens), 0) AS cache_read FROM session_stats_agg WHERE {IN_RANGE}"
                )
            ],
            x=12,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Cache write tokens",
            "stat",
            [
                target(
                    f"SELECT coalesce(sum(cache_write_tokens), 0) AS cache_write FROM session_stats_agg WHERE {IN_RANGE}"
                )
            ],
            x=18,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Tokens over time",
            "timeseries",
            [
                target(
                    f"SELECT {BUCKET} AS time, sum(input_tokens) AS input, sum(output_tokens) AS output "
                    f"FROM session_stats_agg WHERE {IN_RANGE} GROUP BY time ORDER BY time",
                    fmt="timeseries",
                    columns=[*TS, col("input", "number"), col("output", "number")],
                )
            ],
            x=0,
            y=4,
            w=16,
        ),
        panel(
            "Tokens by model",
            "barchart",
            [
                target(
                    "SELECT model, sum(input_tokens + output_tokens) AS tokens FROM session_stats_agg "
                    f"WHERE model <> '' AND {IN_RANGE} GROUP BY model ORDER BY tokens DESC LIMIT 15"
                )
            ],
            x=16,
            y=4,
            w=8,
        ),
        panel(
            "Top users by tokens",
            "table",
            [
                target(
                    "SELECT user_id, count(*) AS sessions, sum(input_tokens) AS input_tokens, "
                    f"sum(output_tokens) AS output_tokens FROM session_stats_agg WHERE {IN_RANGE} "
                    "GROUP BY user_id ORDER BY sum(input_tokens + output_tokens) DESC LIMIT 25"
                )
            ],
            x=0,
            y=12,
            w=24,
            h=9,
        ),
    ]
    return dashboard(
        "observal-token-usage",
        "Observal / Token Usage",
        panels,
        description="Token consumption by time, model, and user.",
    )


def tool_calls() -> dict:
    global _id
    _id = 0
    panels = [
        panel(
            "Tool calls",
            "stat",
            [target(f"SELECT count(*) AS calls FROM session_events WHERE event_type = 'tool_call' AND {EV_RANGE}")],
            x=0,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Tool results",
            "stat",
            [target(f"SELECT count(*) AS results FROM session_events WHERE event_type = 'tool_result' AND {EV_RANGE}")],
            x=8,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Distinct tools",
            "stat",
            [
                target(
                    "SELECT count(DISTINCT tool_name) AS tools FROM session_events "
                    f"WHERE event_type = 'tool_call' AND tool_name IS NOT NULL AND {EV_RANGE}"
                )
            ],
            x=16,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Tool calls over time",
            "timeseries",
            [
                target(
                    f"SELECT {EV_BUCKET} AS time, count(*) AS calls FROM session_events "
                    f"WHERE event_type = 'tool_call' AND {EV_RANGE} GROUP BY time ORDER BY time",
                    fmt="timeseries",
                    columns=[*TS, col("calls", "number")],
                )
            ],
            x=0,
            y=4,
            w=16,
        ),
        panel(
            "Most used tools",
            "piechart",
            [
                target(
                    "SELECT tool_name, count(*) AS calls FROM session_events "
                    f"WHERE event_type = 'tool_call' AND tool_name IS NOT NULL AND {EV_RANGE} "
                    "GROUP BY tool_name ORDER BY calls DESC LIMIT 12"
                )
            ],
            x=16,
            y=4,
            w=8,
        ),
        panel(
            "Tools by harness",
            "table",
            [
                target(
                    "SELECT harness, tool_name, count(*) AS calls FROM session_events "
                    f"WHERE event_type = 'tool_call' AND tool_name IS NOT NULL AND {EV_RANGE} "
                    "GROUP BY harness, tool_name ORDER BY calls DESC LIMIT 100"
                )
            ],
            x=0,
            y=12,
            w=24,
            h=9,
        ),
    ]
    return dashboard(
        "observal-tool-call-frequency",
        "Observal / Tool Call Frequency",
        panels,
        description="Which tools agents call, how often, and from which harness.",
    )


def cost_tracking() -> dict:
    global _id
    _id = 0
    panels = [
        panel(
            "Credits (Kiro)",
            "stat",
            [target(f"SELECT coalesce(sum(total_credits), 0) AS credits FROM session_stats_agg WHERE {IN_RANGE}")],
            x=0,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Total tokens",
            "stat",
            [
                target(
                    "SELECT coalesce(sum(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0) "
                    f"AS tokens FROM session_stats_agg WHERE {IN_RANGE}"
                )
            ],
            x=8,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Avg tokens per session",
            "stat",
            [
                target(
                    "SELECT round(coalesce(avg(input_tokens + output_tokens), 0)) AS avg_tokens "
                    f"FROM session_stats_agg WHERE {IN_RANGE}"
                )
            ],
            x=16,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Credits over time",
            "timeseries",
            [
                target(
                    f"SELECT {BUCKET} AS time, sum(total_credits) AS credits FROM session_stats_agg "
                    f"WHERE {IN_RANGE} GROUP BY time ORDER BY time",
                    fmt="timeseries",
                    columns=[*TS, col("credits", "number")],
                )
            ],
            x=0,
            y=4,
            w=12,
        ),
        panel(
            "Tokens per agent",
            "barchart",
            [
                target(
                    "SELECT agent_id, sum(input_tokens + output_tokens) AS tokens FROM session_stats_agg "
                    f"WHERE agent_id <> '' AND {IN_RANGE} GROUP BY agent_id ORDER BY tokens DESC LIMIT 15"
                )
            ],
            x=12,
            y=4,
            w=12,
        ),
        panel(
            "Cost drivers by user",
            "table",
            [
                target(
                    "SELECT user_id, count(*) AS sessions, sum(total_credits) AS credits, "
                    f"sum(input_tokens + output_tokens) AS tokens FROM session_stats_agg WHERE {IN_RANGE} "
                    "GROUP BY user_id ORDER BY tokens DESC LIMIT 25"
                )
            ],
            x=0,
            y=12,
            w=24,
            h=9,
        ),
    ]
    return dashboard(
        "observal-cost-tracking",
        "Observal / Cost Tracking",
        panels,
        description="Credits and token spend by time, agent, and user.",
    )


def agent_activity() -> dict:
    global _id
    _id = 0
    panels = [
        panel(
            "Active agents",
            "stat",
            [
                target(
                    f"SELECT count(DISTINCT agent_id) AS agents FROM session_stats_agg WHERE agent_id <> '' AND {IN_RANGE}"
                )
            ],
            x=0,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Agent sessions",
            "stat",
            [target(f"SELECT count(*) AS sessions FROM session_stats_agg WHERE agent_id <> '' AND {IN_RANGE}")],
            x=8,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Subagent sessions",
            "stat",
            [
                target(
                    f"SELECT count(*) AS sessions FROM session_stats_agg WHERE parent_session_id <> '' AND {IN_RANGE}"
                )
            ],
            x=16,
            y=0,
            w=8,
            h=4,
        ),
        panel(
            "Agent sessions over time",
            "timeseries",
            [
                target(
                    f"SELECT {BUCKET} AS time, count(*) AS sessions FROM session_stats_agg "
                    f"WHERE agent_id <> '' AND {IN_RANGE} GROUP BY time ORDER BY time",
                    fmt="timeseries",
                    columns=[*TS, col("sessions", "number")],
                )
            ],
            x=0,
            y=4,
            w=16,
        ),
        panel(
            "Sessions by agent",
            "barchart",
            [
                target(
                    "SELECT agent_id, count(*) AS sessions FROM session_stats_agg "
                    f"WHERE agent_id <> '' AND {IN_RANGE} GROUP BY agent_id ORDER BY sessions DESC LIMIT 15"
                )
            ],
            x=16,
            y=4,
            w=8,
        ),
        panel(
            "Agent versions in use",
            "table",
            [
                target(
                    "SELECT agent_id, agent_version, count(*) AS sessions, count(DISTINCT user_id) AS users, "
                    f"max(last_event_time) AS last_seen FROM session_stats_agg WHERE agent_id <> '' AND {IN_RANGE} "
                    "GROUP BY agent_id, agent_version ORDER BY sessions DESC LIMIT 50"
                )
            ],
            x=0,
            y=12,
            w=24,
            h=9,
        ),
    ]
    return dashboard(
        "observal-agent-activity",
        "Observal / Agent Activity",
        panels,
        description="Registered-agent usage, versions, and subagent activity.",
    )


def audit_log() -> dict:
    global _id
    _id = 0
    rng = EV_RANGE
    bucket = EV_BUCKET
    panels = [
        panel(
            "Audit events",
            "stat",
            [target(f"SELECT count(*) AS events FROM audit_log WHERE {rng}")],
            x=0,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Failures",
            "stat",
            [target(f"SELECT count(*) AS failures FROM audit_log WHERE outcome = 'failure' AND {rng}")],
            x=6,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Sensitive actions",
            "stat",
            [target(f"SELECT count(*) AS sensitive FROM audit_log WHERE sensitivity <> 'standard' AND {rng}")],
            x=12,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Security events",
            "stat",
            [target(f"SELECT count(*) AS events FROM security_events WHERE {rng}")],
            x=18,
            y=0,
            w=6,
            h=4,
        ),
        panel(
            "Audit events over time",
            "timeseries",
            [
                target(
                    f"SELECT {bucket} AS time, count(*) AS events FROM audit_log WHERE {rng} GROUP BY time ORDER BY time",
                    fmt="timeseries",
                    columns=[*TS, col("events", "number")],
                )
            ],
            x=0,
            y=4,
            w=16,
        ),
        panel(
            "Actions",
            "piechart",
            [
                target(
                    f"SELECT action, count(*) AS events FROM audit_log WHERE {rng} GROUP BY action ORDER BY events DESC LIMIT 12"
                )
            ],
            x=16,
            y=4,
            w=8,
        ),
        panel(
            "Recent audit entries",
            "table",
            [
                target(
                    'SELECT "timestamp" AS time, actor_email, action, resource_type, resource_name, outcome, source, '
                    f'http_method, http_path, status_code FROM audit_log WHERE {rng} ORDER BY "timestamp" DESC LIMIT 100'
                )
            ],
            x=0,
            y=12,
            w=24,
            h=10,
        ),
        panel(
            "Recent security events",
            "table",
            [
                target(
                    f'SELECT "timestamp" AS time, event_type, severity, actor_email, outcome, source_ip, detail '
                    f'FROM security_events WHERE {rng} ORDER BY "timestamp" DESC LIMIT 50'
                )
            ],
            x=0,
            y=22,
            w=24,
            h=8,
        ),
    ]
    return dashboard(
        "observal-audit-log",
        "Observal / Audit Log",
        panels,
        description="Audit trail and security events from the telemetry store.",
    )


GENERATORS = {
    "session-overview.json": session_overview,
    "token-usage.json": token_usage,
    "tool-call-frequency.json": tool_calls,
    "cost-tracking.json": cost_tracking,
    "agent-activity.json": agent_activity,
    "audit-log.json": audit_log,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, gen in GENERATORS.items():
        path = OUT / name
        path.write_text(json.dumps(gen(), indent=2) + "\n")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
