# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Every Grafana panel query must run against the telemetry store schema (acceptance A-24 subset)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = sorted(p for p in (ROOT / "grafana" / "dashboards").glob("*.json") if p.name != "self-observability.json")


def _panel_queries(path: Path) -> list[tuple[str, dict]]:
    doc = json.loads(path.read_text())
    out = []
    for panel in doc["panels"]:
        assert panel["datasource"]["uid"] == "observal-telemetry", path.name
        for t in panel["targets"]:
            assert t["url"] == "${telemetry_url}/v1/query"
            body = json.loads(t["url_options"]["data"])
            out.append((panel["title"], body))
    return out


def test_generator_output_is_committed():
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts" / "gen_grafana_dashboards.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    for name, builder in gen.GENERATORS.items():
        assert json.loads((gen.OUT / name).read_text()) == builder(), f"{name} is stale; run the generator"


def test_datasource_provisioning_uses_infinity():
    text = (ROOT / "grafana" / "provisioning" / "datasources" / "telemetry.yaml").read_text()
    assert "yesoreyeram-infinity-datasource" in text
    assert "uid: observal-telemetry" in text
    assert "$TELEMETRY_TOKEN" in text
    assert not list((ROOT / "grafana" / "provisioning" / "datasources").glob("clickhouse*"))


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
async def test_every_panel_query_runs(telemetry, path):
    # Seed a little data so aggregates have rows to chew on.
    events = [
        {
            "session_id": "g1",
            "project_id": "default",
            "user_id": "u1",
            "harness": "claude-code",
            "agent_id": "agent-a",
            "agent_version": "1.0.0",
            "line_offset": i,
            "line_hash": f"h{i}",
            "event_type": ["user_prompt", "tool_call", "tool_result"][i % 3],
            "tool_name": "Bash" if i % 3 == 1 else None,
            "timestamp": "2026-05-01 10:00:00.000",
            "input_tokens": 3,
            "output_tokens": 2,
            "model": "claude",
            "credits": 1.5,
        }
        for i in range(6)
    ]
    r = await telemetry.post(
        "/v1/write/session-batch",
        json={
            "events": events,
            "advance_checkpoint": {
                "project_id": "default",
                "user_id": "u1",
                "harness": "claude-code",
                "session_id": "g1",
            },
        },
    )
    assert r.status_code == 200, r.text
    await telemetry.post(
        "/v1/write/append",
        json={
            "table": "audit_log",
            "rows": [
                {"event_id": "11111111-1111-1111-1111-111111111111", "timestamp": "2026-05-01 10:00:00", "action": "x"}
            ],
        },
    )
    variables = {
        "${__from:date:iso}": "2026-04-01T00:00:00Z",
        "${__to:date:iso}": "2026-06-01T00:00:00Z",
        "${bucket}": "1 hour",
    }
    for title, body in _panel_queries(path):
        params = {k: variables.get(v, v) for k, v in body["params"].items()}
        resp = await telemetry.post("/v1/query", json={"sql": body["sql"], "params": params})
        assert resp.status_code == 200, f"{path.name} / {title}: {resp.text}"
