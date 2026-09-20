# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Apply the telemetry schema without starting the HTTP service.

Used by init containers: ``python -m telemetry_store.migrate``. Refuses to run
while the service owns the file (single-writer lock) unless the service is
reachable, in which case the schema is already applied at service start.
"""

from __future__ import annotations

import sys

from loguru import logger as optic

from telemetry_store.db import Database, SingleWriterViolationError
from telemetry_store.settings import TelemetrySettings


def main() -> int:
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


if __name__ == "__main__":
    sys.exit(main())
