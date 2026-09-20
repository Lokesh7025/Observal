# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Map telemetry store failures to HTTP responses. No route swallows them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse
from loguru import logger as optic

from services.telemetry.client import TelemetryError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


def configure_telemetry_errors(app: FastAPI) -> None:
    @app.exception_handler(TelemetryError)
    async def _telemetry_error(request: Request, exc: TelemetryError) -> JSONResponse:
        optic.error("telemetry failure on {} {}: {}", request.method, request.url.path, exc)
        headers = {"Retry-After": "5"} if exc.status_code in (429, 503) else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.code, "message": str(exc)[:500]},
            headers=headers,
        )
