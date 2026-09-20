# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Layer snapshot routes against a real telemetry store."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.deps import get_current_user, get_db
from api.ratelimit import limiter
from api.routes import layer_snapshot
from models.user import UserRole
from observal_shared.migration.constants import DEFAULT_PROJECT_ID

USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
HASH_A = "a" * 40
HASH_B = "b" * 40


def _user(*, user_id: uuid.UUID = USER_ID, role: UserRole = UserRole.user):
    return SimpleNamespace(id=user_id, role=role, email="member@example.test", username="member", auth_provider="local")


def _app(user=None, *, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    app.include_router(layer_snapshot.router)
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: user or _user()
    return app


async def _request(app: FastAPI, method: str, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


def _file(path: str, file_hash: str, size: int, *, source: str = "user", content: str = "") -> dict:
    return {"path": path, "hash": file_hash, "size": size, "source": source, "content": content}


def _snapshot(snapshot_hash: str, files: dict[str, list[dict]], **extra) -> dict:
    return {"hash": snapshot_hash, "harnesses": files, "lockfile_hash": "lock-1", **extra}


@pytest.fixture(autouse=True)
def _disable_rate_limits():
    enabled = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = enabled


async def _rows(telemetry, sql: str, **params):
    r = await telemetry.post("/v1/query", json={"sql": sql, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


async def test_upload_stores_redacts_and_dedupes(telemetry):
    app = _app()
    secret = "sk-proj-abc123def456ghi789jkl012mno345"
    body = _snapshot(
        HASH_A,
        {
            "claude-code": [_file("CLAUDE.md", "f1", 10, content=f"token {secret}")],
            "pi": [_file("AGENTS.md", "f2", 20, content="plain")],
        },
        pinned_versions={"agent": "1.2.0"},
        drift={"dirty": True},
    )

    r = await _request(app, "POST", "/api/v1/layer-snapshots", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"stored": True, "hash": HASH_A, "file_count": 2}

    rows = await _rows(
        telemetry,
        "SELECT hash, project_id, user_id, harness, content, file_count, total_size, lockfile_hash FROM layer_snapshots",
    )
    assert len(rows) == 1
    row = rows[0]
    assert (row["project_id"], row["user_id"], row["harness"]) == (DEFAULT_PROJECT_ID, str(USER_ID), "claude-code,pi")
    assert (row["file_count"], row["total_size"], row["lockfile_hash"]) == (2, 30, "lock-1")
    content = json.loads(row["content"])
    assert secret not in row["content"]
    assert content["pinned_versions"] == {"agent": "1.2.0"} and content["drift"] == {"dirty": True}

    # Same hash again: reported as already stored, no duplicate row.
    r = await _request(app, "POST", "/api/v1/layer-snapshots", json=body)
    assert r.json() == {"stored": False, "hash": HASH_A, "file_count": 2}
    assert await _rows(telemetry, "SELECT count(*) AS c FROM layer_snapshots") == [{"c": 1}]


async def test_upload_limits_and_auth(telemetry):
    app = _app()
    too_many = _snapshot(HASH_B, {"pi": [_file(f"f{i}", f"h{i}", 1) for i in range(201)]})
    r = await _request(app, "POST", "/api/v1/layer-snapshots", json=too_many)
    assert r.status_code == 422 and "file limit" in r.text

    big = _snapshot(HASH_B, {"pi": [_file(f"f{i}", f"h{i}", 1, content="x" * 524288) for i in range(11)]})
    r = await _request(app, "POST", "/api/v1/layer-snapshots", json=big)
    assert r.status_code == 422 and "total content limit" in r.text

    r = await _request(_app(authenticated=False), "POST", "/api/v1/layer-snapshots", json=_snapshot(HASH_B, {}))
    assert r.status_code == 401
    assert await _rows(telemetry, "SELECT count(*) AS c FROM layer_snapshots") == [{"c": 0}]


async def test_get_and_diff_snapshots(telemetry):
    app = _app()
    a = _snapshot(HASH_A, {"pi": [_file("same.md", "s1", 1), _file("changed.md", "c1", 2), _file("gone.md", "g1", 3)]})
    b = _snapshot(HASH_B, {"pi": [_file("same.md", "s1", 1), _file("changed.md", "c2", 2), _file("new.md", "n1", 4)]})
    for body in (a, b):
        assert (await _request(app, "POST", "/api/v1/layer-snapshots", json=body)).status_code == 200

    r = await _request(app, "GET", f"/api/v1/layer-snapshots/{HASH_A}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["hash"] == HASH_A and detail["harness"] == "pi"
    assert [f["path"] for f in detail["files"]] == ["same.md", "changed.md", "gone.md"]
    assert detail["file_count"] == 3 and detail["total_size"] == 6 and detail["lockfile_hash"] == "lock-1"
    assert detail["uploaded_at"].count(":") == 2

    r = await _request(app, "GET", f"/api/v1/layer-snapshots/{'0' * 40}")
    assert r.status_code == 404

    r = await _request(app, "GET", f"/api/v1/layer-snapshots/{HASH_A}/diff/{HASH_B}")
    assert r.status_code == 200, r.text
    diff = r.json()
    assert [f["path"] for f in diff["added"]] == ["new.md"]
    assert [f["path"] for f in diff["removed"]] == ["gone.md"]
    assert [f["path"] for f in diff["modified"]] == ["pi/changed.md"]
    assert diff["unchanged_count"] == 1

    r = await _request(app, "GET", f"/api/v1/layer-snapshots/{HASH_A}/diff/{'0' * 40}")
    assert r.status_code == 404


async def test_pin_baseline_is_idempotent_per_user(telemetry):
    app = _app()
    body = {"agent_id": "reviewer", "layer_hash": HASH_A}
    r = await _request(app, "POST", "/api/v1/layer-snapshots/baseline", json=body)
    assert r.status_code == 200 and r.json() == {"agent_id": "reviewer", "layer_hash": HASH_A, "pinned": True}
    r = await _request(app, "POST", "/api/v1/layer-snapshots/baseline", json={**body, "layer_hash": HASH_B})
    assert r.status_code == 200

    rows = await _rows(telemetry, "SELECT hash, harness, user_id, content FROM layer_snapshots ORDER BY user_id")
    assert len(rows) == 1
    assert rows[0]["hash"] == "baseline:reviewer" and rows[0]["harness"] == "baseline"
    assert json.loads(rows[0]["content"])["pinned_hash"] == HASH_B

    other = _app(_user(user_id=OTHER_ID))
    assert (await _request(other, "POST", "/api/v1/layer-snapshots/baseline", json=body)).status_code == 200
    assert await _rows(telemetry, "SELECT count(*) AS c FROM layer_snapshots") == [{"c": 2}]

    r = await _request(
        app, "POST", "/api/v1/layer-snapshots/baseline", json={"agent_id": "x" * 101, "layer_hash": HASH_A}
    )
    assert r.status_code == 422


async def test_store_outage_is_a_503_not_a_silent_success(telemetry):
    import httpx

    from services.telemetry import client as tclient
    from services.telemetry.errors import configure_telemetry_errors

    class _Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("down")

    tclient.set_client(httpx.AsyncClient(transport=_Down(), base_url="http://telemetry"))
    app = _app()
    configure_telemetry_errors(app)

    r = await _request(app, "POST", "/api/v1/layer-snapshots", json=_snapshot(HASH_A, {"pi": [_file("a", "h", 1)]}))
    assert r.status_code == 503 and r.json()["detail"] == "telemetry_unavailable"
    r = await _request(app, "GET", f"/api/v1/layer-snapshots/{HASH_A}")
    assert r.status_code == 503
    r = await _request(app, "POST", "/api/v1/layer-snapshots/baseline", json={"agent_id": "a", "layer_hash": HASH_A})
    assert r.status_code == 503
