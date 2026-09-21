# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Create an online, snapshot-consistent copy of the telemetry database.

Usage (inside the telemetry container): ``python -m telemetry_store.backup <dest_path>``

The service creates the snapshot beneath its configured backup root. This
trusted local helper downloads it to the requested destination afterward, so
remote HTTP callers can never choose a server filesystem path.
"""

from __future__ import annotations

import hashlib
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
    temporary = dest.with_suffix(dest.suffix + ".partial")
    temporary.unlink(missing_ok=True)
    try:
        with httpx.Client(base_url=base, headers=headers, timeout=60.0) as client:
            resp = client.post("/v1/admin/backup")
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
                    break
                if job["state"] == "failed":
                    print(f"backup failed: {job.get('error')}", file=sys.stderr)
                    return 1
                time.sleep(1.0)

            digest = hashlib.sha256()
            size = 0
            with client.stream("GET", f"/v1/admin/backup/{job_id}/file", timeout=None) as download:
                if download.status_code >= 400:
                    download.read()
                    print(f"backup download failed: {download.status_code} {download.text[:300]}", file=sys.stderr)
                    return 1
                with temporary.open("wb") as output:
                    for block in download.iter_bytes(1 << 20):
                        output.write(block)
                        digest.update(block)
                        size += len(block)
            expected = job["result"]
            if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                print("backup download failed integrity verification", file=sys.stderr)
                return 1
            temporary.chmod(0o600)
            temporary.replace(dest)
            print(f"backup written: {dest} ({size} bytes)")
            print(f"sha256: {digest.hexdigest()}")
            return 0
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
