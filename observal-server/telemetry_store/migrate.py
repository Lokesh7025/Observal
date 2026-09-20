# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Verify (or apply) the telemetry schema without starting the HTTP service.

Used by init containers: ``python -m telemetry_store.migrate``.

Two modes, chosen by ``TELEMETRY_URL``:

* **Service mode** (``TELEMETRY_URL`` set, the normal Compose/Helm case): the
  init container does not share the data volume with the store, so it never
  opens the database file. It waits for the running service to report a
  healthy schema and exits non-zero if the service does not come up within
  ``TELEMETRY_MIGRATE_WAIT_S`` seconds.
* **File mode** (no ``TELEMETRY_URL``): apply ``001_baseline.sql`` directly to
  ``TELEMETRY_DB_PATH``. Used by embedded installs and manual repair. Refuses
  to touch a file another process currently owns.
"""

from __future__ import annotations

import os
import sys
import time

import httpx
from loguru import logger as optic

from telemetry_store.db import Database, SingleWriterViolationError
from telemetry_store.settings import TelemetrySettings


def _wait_for_service(url: str, token: str, wait_s: float) -> int:
    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.monotonic() + wait_s
    last_error = "not attempted"
    with httpx.Client(timeout=5.0) as client:
        while True:
            try:
                resp = client.get(f"{base}/v1/health", headers=headers)
                if resp.status_code == 200:
                    body = resp.json()
                    optic.info(
                        "telemetry service healthy at {} (schema={}, duckdb={})",
                        base,
                        body.get("schema_version"),
                        body.get("duckdb_version"),
                    )
                    return 0
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                optic.error("telemetry service at {} not healthy after {}s: {}", base, wait_s, last_error)
                return 1
            time.sleep(2.0)


def _apply_local() -> int:
    settings = TelemetrySettings.from_env()
    database = Database(settings)
    try:
        applied = database.apply_schema()
    except SingleWriterViolationError:
        optic.info("telemetry service owns {}; schema is applied at service start", settings.db_path)
        return 0
    finally:
        database.close()
    if applied:
        optic.info("telemetry schema applied: {}", applied)
    else:
        optic.info("telemetry schema up to date")
    return 0


def main() -> int:
    url = os.environ.get("TELEMETRY_URL", "").strip()
    if url:
        wait_s = float(os.environ.get("TELEMETRY_MIGRATE_WAIT_S", "120"))
        return _wait_for_service(url, os.environ.get("TELEMETRY_TOKEN", ""), wait_s)
    return _apply_local()


if __name__ == "__main__":
    sys.exit(main())
