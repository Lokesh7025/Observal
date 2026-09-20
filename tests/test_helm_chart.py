# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Helm chart renders the telemetry store and gates ClickHouse behind the legacy flag (acceptance A-21)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "infra" / "helm" / "observal"
HELM = shutil.which("helm")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm binary not available")


def _render(*extra: str) -> list[dict]:
    out = subprocess.run(
        [HELM, "template", "observal", str(CHART), "--namespace", "observal", *extra],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(out) if doc]


def _named(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == kind}


def test_default_render_has_telemetry_and_no_clickhouse():
    docs = _render()
    sets = _named(docs, "StatefulSet")
    assert "observal-telemetry" in sets and "observal-clickhouse" not in sets
    telemetry = sets["observal-telemetry"]
    assert telemetry["spec"]["replicas"] == 1
    container = telemetry["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["/app/.venv/bin/python", "-m", "telemetry_store"]
    assert container["readinessProbe"]["httpGet"]["path"] == "/v1/health"
    secret = _named(docs, "Secret")["observal-secret"]["stringData"]
    assert secret["TELEMETRY_URL"] == "http://observal-telemetry:8125"
    assert secret["TELEMETRY_TOKEN"]
    assert "CLICKHOUSE_URL" not in secret
    for name in ("observal-api", "observal-worker"):
        inits = [c["name"] for c in _named(docs, "Deployment")[name]["spec"]["template"]["spec"]["initContainers"]]
        assert "wait-for-telemetry" in inits and "wait-for-clickhouse" not in inits


def test_legacy_flag_adds_clickhouse_for_the_cutover_window():
    docs = _render("--set", "clickhouse.legacy.enabled=true")
    sets = _named(docs, "StatefulSet")
    assert {"observal-telemetry", "observal-clickhouse"} <= set(sets)
    secret = _named(docs, "Secret")["observal-secret"]["stringData"]
    assert secret["CLICKHOUSE_URL"].startswith("clickhouse://default:")
    assert secret["TELEMETRY_URL"] == "http://observal-telemetry:8125"


def test_external_telemetry_requires_url():
    result = subprocess.run(
        [HELM, "template", "observal", str(CHART), "--set", "telemetry.enabled=false"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "telemetry.externalUrl is required" in result.stderr
    docs = _render("--set", "telemetry.enabled=false", "--set", "telemetry.externalUrl=http://tel:8125")
    assert "observal-telemetry" not in _named(docs, "StatefulSet")
    assert _named(docs, "Secret")["observal-secret"]["stringData"]["TELEMETRY_URL"] == "http://tel:8125"


def test_chart_lints():
    subprocess.run([HELM, "lint", str(CHART)], check=True, capture_output=True)
