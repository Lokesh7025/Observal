# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""HTTP client for the telemetry store.

Every failure raises. Nothing here returns an empty result on error: callers
either succeed or surface ``TelemetryError`` which the API maps to 503/504/413.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger as optic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import settings


class TelemetryError(RuntimeError):
    """Base class for telemetry store failures."""

    status_code = 503
    code = "telemetry_error"


class TelemetryUnavailableError(TelemetryError):
    """Connection failure, 5xx, or writer pause."""

    status_code = 503
    code = "telemetry_unavailable"


class TelemetryQueryError(TelemetryError):
    """The store rejected the request (bad SQL, bad rows, unknown table)."""

    status_code = 500
    code = "telemetry_query_error"


class TelemetryTimeoutError(TelemetryError):
    status_code = 504
    code = "telemetry_timeout"


class TelemetryResultTooLargeError(TelemetryError):
    status_code = 413
    code = "telemetry_result_too_large"


class TelemetryBusyError(TelemetryError):
    status_code = 429
    code = "telemetry_busy"


_client: httpx.AsyncClient | None = None
_base_url: str = ""


def _headers() -> dict[str, str]:
    token = settings.TELEMETRY_TOKEN
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_client() -> httpx.AsyncClient:
    global _client, _base_url
    if _client is None:
        _base_url = settings.TELEMETRY_URL.rstrip("/")
        optic.debug(
            "creating telemetry HTTP client (url={}, timeout={}s, pool={})",
            _base_url,
            settings.TELEMETRY_TIMEOUT,
            settings.TELEMETRY_MAX_CONNECTIONS,
        )
        _client = httpx.AsyncClient(
            base_url=_base_url,
            timeout=httpx.Timeout(settings.TELEMETRY_TIMEOUT, connect=5.0),
            limits=httpx.Limits(
                max_connections=settings.TELEMETRY_MAX_CONNECTIONS,
                max_keepalive_connections=settings.TELEMETRY_MAX_CONNECTIONS,
            ),
            headers=_headers(),
        )
    return _client


def set_client(client: httpx.AsyncClient | None) -> None:
    """Install a client (tests inject an ASGI-transport client)."""
    global _client
    _client = client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _raise_for(resp: httpx.Response, sql_preview: str = "") -> None:
    if resp.status_code < 400:
        return
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    code = err.get("code", "")
    message = err.get("message", resp.text[:300])
    detail = f"{code or resp.status_code}: {message}"
    if sql_preview:
        detail += f" (sql: {sql_preview[:120]})"
    if resp.status_code == 504 or code == "query_timeout":
        raise TelemetryTimeoutError(detail)
    if resp.status_code == 413:
        raise TelemetryResultTooLargeError(detail)
    if resp.status_code == 429:
        raise TelemetryBusyError(detail)
    if resp.status_code in (401, 400, 422) or code in {"query_rejected", "query_error", "write_rejected"}:
        raise TelemetryQueryError(detail)
    raise TelemetryUnavailableError(detail)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.3, min=0.3, max=3),
    retry=retry_if_exception_type((httpx.ConnectError, httpx.ConnectTimeout)),
    reraise=True,
)
async def _post(path: str, payload: dict[str, Any], *, timeout: float | None = None) -> httpx.Response:
    client = get_client()
    return await client.post(path, json=payload, timeout=timeout)


async def query(sql: str, params: dict[str, Any] | None = None, *, timeout_ms: int | None = None) -> list[dict]:
    """Run a read-only query and return rows as dicts. Raises on any failure."""
    t0 = time.perf_counter()
    payload: dict[str, Any] = {"sql": sql, "params": params or {}}
    if timeout_ms:
        payload["timeout_ms"] = timeout_ms
    http_timeout = (timeout_ms / 1000 + 5) if timeout_ms else None
    try:
        resp = await _post("/v1/query", payload, timeout=http_timeout)
    except httpx.HTTPError as exc:
        optic.error("telemetry query transport failure after {:.0f}ms: {}", (time.perf_counter() - t0) * 1000, exc)
        raise TelemetryUnavailableError(f"telemetry store unreachable: {type(exc).__name__}") from exc
    _raise_for(resp, sql)
    body = resp.json()
    optic.trace("telemetry query ok ({} rows, {:.0f}ms)", body.get("row_count"), (time.perf_counter() - t0) * 1000)
    return body["rows"]


async def query_one(sql: str, params: dict[str, Any] | None = None) -> dict:
    rows = await query(sql, params)
    return rows[0] if rows else {}


async def scalar(sql: str, params: dict[str, Any] | None = None, default: Any = 0) -> Any:
    row = await query_one(sql, params)
    if not row:
        return default
    value = next(iter(row.values()))
    return default if value is None else value


async def write(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to a write endpoint. Raises on any failure."""
    t0 = time.perf_counter()
    try:
        resp = await _post(path, payload, timeout=settings.TELEMETRY_WRITE_TIMEOUT)
    except httpx.HTTPError as exc:
        optic.error(
            "telemetry write {} transport failure after {:.0f}ms: {}", path, (time.perf_counter() - t0) * 1000, exc
        )
        raise TelemetryUnavailableError(f"telemetry store unreachable: {type(exc).__name__}") from exc
    _raise_for(resp)
    return resp.json()


async def get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        resp = await get_client().get(path, params=params)
    except httpx.HTTPError as exc:
        raise TelemetryUnavailableError(f"telemetry store unreachable: {type(exc).__name__}") from exc
    _raise_for(resp)
    return resp.json()


async def health() -> dict[str, Any] | None:
    """Return the store's health document, or ``None`` when unreachable."""
    t0 = time.perf_counter()
    try:
        resp = await get_client().get("/v1/health", timeout=5.0)
    except httpx.HTTPError as exc:
        optic.error("telemetry store unreachable after {:.0f}ms: {}", (time.perf_counter() - t0) * 1000, exc)
        return None
    if resp.status_code != 200:
        optic.warning("telemetry health returned {}", resp.status_code)
        return None
    return resp.json()


async def telemetry_health() -> bool:
    return (await health()) is not None


async def verify_telemetry() -> None:
    """Startup check: the store must be reachable and on the expected schema."""
    status = await health()
    if status is None:
        raise RuntimeError(f"telemetry store unreachable at {settings.TELEMETRY_URL}")
    optic.info(
        "telemetry store ready (schema={}, duckdb={}, file_bytes={})",
        status.get("schema_version"),
        status.get("duckdb_version"),
        status.get("file_bytes"),
    )
