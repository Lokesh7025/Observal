# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Entry point: ``python -m telemetry_store``.

Refuses to run with more than one worker: the store is single-writer by design.
"""

from __future__ import annotations

import os
import sys

import uvicorn
from loguru import logger as optic

from telemetry_store.api import create_app
from telemetry_store.settings import TelemetrySettings


def _reject_multi_worker(argv: list[str]) -> None:
    workers = os.environ.get("WEB_CONCURRENCY") or os.environ.get("UVICORN_WORKERS")
    for flag in ("--workers", "-w"):
        if flag in argv:
            workers = argv[argv.index(flag) + 1]
    if workers and int(workers) > 1:
        optic.error("telemetry store is single-writer; refusing to start with {} workers", workers)
        sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    _reject_multi_worker(argv)
    settings = TelemetrySettings.from_env()
    settings.validate()
    app = create_app(settings)
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, workers=1, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
