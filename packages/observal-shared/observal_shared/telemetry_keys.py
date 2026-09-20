# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Deterministic integer keys for telemetry identities.

The DuckDB telemetry store has no indexes (by design). Every identity lookup
filters on a fixed-width ``BIGINT`` column instead of on several string
columns, which keeps 30M-row scans in the tens of milliseconds. This module is
the single source of truth for those keys and is shared by the API client, the
telemetry service, and the migration importer.
"""

from __future__ import annotations

import xxhash

_SEP = "\x1f"
_SIGN_BIT = 1 << 63
_MASK = (1 << 64) - 1


def _to_signed(value: int) -> int:
    value &= _MASK
    return value - (1 << 64) if value & _SIGN_BIT else value


def identity_key(*parts: str) -> int:
    """Hash an ordered tuple of identity strings to a signed 64-bit integer."""
    joined = _SEP.join("" if part is None else str(part) for part in parts)
    return _to_signed(xxhash.xxh3_64_intdigest(joined.encode("utf-8", errors="replace")))


def session_key(project_id: str, user_id: str, harness: str, session_id: str) -> int:
    """Key for one session source: ``(project_id, user_id, harness, session_id)``."""
    return identity_key(project_id, user_id, harness, session_id)


def parent_session_key(project_id: str, user_id: str, harness: str, parent_session_id: str | None) -> int | None:
    """Key of the parent session for subagent rows; ``None`` when there is no parent."""
    if not parent_session_id:
        return None
    return session_key(project_id, user_id, harness, parent_session_id)


def snapshot_key(project_id: str, user_id: str, snapshot_hash: str) -> int:
    """Key for one layer snapshot: ``(project_id, user_id, hash)``."""
    return identity_key(project_id, user_id, snapshot_hash)


__all__ = ["identity_key", "parent_session_key", "session_key", "snapshot_key"]
