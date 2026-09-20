# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Small SQL builders for DuckDB telemetry queries."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

_TS_SENTINEL_CUTOFF = datetime(2099, 1, 1, tzinfo=UTC)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Sentinel bounds preserved from the ClickHouse implementation.
VALID_TS = "\"timestamp\" > TIMESTAMP '1971-01-01 00:00:00' AND \"timestamp\" < TIMESTAMP '2099-01-01 00:00:00'"
FAR_FUTURE = "TIMESTAMP '2099-01-01 00:00:00'"


def in_condition(column: str, values: list[str], prefix: str, params: dict[str, Any]) -> str | None:
    """``column IN ($p_0, $p_1, …)`` with parameters added to *params*."""
    if not values:
        return None
    if not _IDENT_RE.match(prefix):
        raise ValueError(f"invalid parameter prefix: {prefix}")
    names = []
    for idx, value in enumerate(values):
        name = f"{prefix}_{idx}"
        params[name] = value
        names.append(f"${name}")
    return f"{column} IN ({', '.join(names)})"


def interval_ago(unit: str, param: str) -> str:
    """``now() - INTERVAL ($param) unit`` as a naive UTC TIMESTAMP expression."""
    if unit not in {"minute", "hour", "day"}:
        raise ValueError(f"unsupported interval unit: {unit}")
    if not _IDENT_RE.match(param):
        raise ValueError(f"invalid parameter name: {param}")
    return f"(current_timestamp::TIMESTAMP - to_{unit}s(CAST(${param} AS INTEGER)))"


def now_ts() -> str:
    return "current_timestamp::TIMESTAMP"


def now_ms() -> str:
    """Current UTC timestamp as ``YYYY-MM-DD HH:MM:SS.mmm``."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def normalize_ts(value: str | None) -> str | None:
    """Normalize ISO/space timestamps to ``YYYY-MM-DD HH:MM:SS.mmm`` and clamp far-future sentinels to now."""
    if value is None:
        return None
    v = value.replace("T", " ").rstrip("Z")
    if "+" in v[10:]:
        v = v[: v.rindex("+")]
    if "." not in v:
        v += ".000"
    try:
        parsed = datetime.fromisoformat(v.replace(" ", "T") + "+00:00")
        if parsed >= _TS_SENTINEL_CUTOFF:
            v = now_ms()
    except ValueError:
        pass
    return v


__all__ = ["FAR_FUTURE", "VALID_TS", "in_condition", "interval_ago", "normalize_ts", "now_ms", "now_ts"]
