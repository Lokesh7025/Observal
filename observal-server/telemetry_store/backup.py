# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Create an online, snapshot-consistent copy of the telemetry database.

Usage (inside the telemetry container): ``python -m telemetry_store.backup /data/telemetry/backups/x.duckdb``

Talks to the running service over HTTP so the single writer keeps ownership of
the file; the copy is produced by ``COPY FROM DATABASE`` under one transaction.
Exits non-zero on failure. No interactive timeout: the job is polled until done.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

from telemetry_store.settings import TelemetrySettings


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m telemetry_store.backup <dest_path>", file=sys.stderr)
        return 2
    dest = Path(argv[0])
    settings = TelemetrySettings.from_env()
    base = f"http://127.0.0.1:{settings.bind_port}"
    headers = {"Authorization": f"Bearer {settings.token}"} if settings.token else {}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base, headers=headers, timeout=60.0) as client:
        resp = client.post("/v1/admin/backup", json={"dest_path": str(dest)})
        if resp.status_code >= 400:
            print(f"backup request failed: {resp.status_code} {resp.text[:300]}", file=sys.stderr)
            return 1
        job_id = resp.json()["id"]
        last = None
        while True:
            job = client.get(f"/v1/jobs/{job_id}").json()
            if job["message"] != last:
                last = job["message"]
                print(f"[{job['pct']:3d}%] {last}", flush=True)
            if job["state"] == "done":
                print(f"backup written: {job['result']['path']} ({job['result']['bytes']} bytes)")
                print(f"sha256: {job['result']['sha256']}")
                return 0
            if job["state"] == "failed":
                print(f"backup failed: {job.get('error')}", file=sys.stderr)
                return 1
            time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
