# SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import sys
from pathlib import Path

# Add server source to path so `from config import settings` works
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "observal-server"))
sys.path.insert(0, str(ROOT / "packages" / "observal-shared"))


# ── Telemetry store fixture (real in-process DuckDB behind ASGI) ──────

import pytest_asyncio  # noqa: E402


@pytest_asyncio.fixture()
async def telemetry(tmp_path):
    """Run a real telemetry store and route ``services.telemetry`` to it.

    Yields an httpx client bound to the store so tests can seed data with raw
    writes and inspect tables with raw read-only queries.
    """
    import httpx

    from services.telemetry import client as tclient
    from telemetry_store.api import create_app
    from telemetry_store.settings import TelemetrySettings

    settings = TelemetrySettings(
        db_path=tmp_path / "telemetry.duckdb",
        temp_dir=tmp_path / "tmp",
        export_dir=tmp_path / "exports",
        token="test-token",
        memory_limit="512MB",
        threads=2,
        read_threads=2,
        read_queue_max=16,
        query_timeout_ms=10_000,
        write_queue_timeout_ms=1_000,
        max_result_rows=100_000,
        checkpoint_interval_s=300,
        checkpoint_threshold="16MB",
        bind_host="127.0.0.1",
        bind_port=0,
        metrics_public=False,
        allow_anonymous=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://telemetry", headers={"Authorization": "Bearer test-token"}
        ) as http:
            tclient.set_client(http)
            try:
                yield http
            finally:
                tclient.set_client(None)
