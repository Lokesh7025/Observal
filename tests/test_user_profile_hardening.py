# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Adversarial tests for the ClickHouse array literal built from session ids.

Session ids arrive from client-supplied ingest payloads, so they are
untrusted input that ends up inside a hand-built ClickHouse array literal.
These tests pin the allowlist behaviour: anything that could alter the shape
of that literal must be dropped, never escaped-and-kept.
"""

from __future__ import annotations

import pytest

from services.user_profile import _id_list, _mcp_server_name, _topics_for, users_with_recent_activity

# Each of these, if admitted, would change how ClickHouse parses the literal.
HOSTILE_IDS = [
    "a'",  # bare quote
    "a\\",  # trailing backslash escapes the closing quote
    "a\\'",  # escaped quote
    "a','b",  # element separator
    "') OR 1=1 --",  # classic break-out
    "a]",  # closes the array
    "a[",  # opens an array
    "a,b",  # separator without quotes
    "a b",  # whitespace
    "a\nb",  # newline
    "a\tb",
    "a\x00b",  # null byte
    "/*c*/",  # slashes are not in the charset
    "a`b",
    'a"b',
    "ünïcodé",  # non-ascii
    "a" * 500,  # over length
    "",  # empty
]


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_session_ids_are_dropped(hostile):
    assert _id_list([hostile]) == [], f"{hostile!r} survived into the parameter list"


def test_legitimate_ids_survive():
    ids = [
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "session_123",
        "abc.def",
        "a:b",
        "ABC-123",
    ]
    assert _id_list(ids) == ids


def test_hostile_ids_do_not_contaminate_good_ones():
    """One bad id must not drop the rest."""
    assert _id_list(["good-1", "a\\", "good-2", "') OR 1=1 --"]) == ["good-1", "good-2"]


def test_sql_metacharacters_inside_the_charset_are_inert():
    """`--` is admitted: ids are bound as a list parameter, never interpolated."""
    assert _id_list(["--comment"]) == ["--comment"]


def test_empty_input_yields_empty_list():
    assert _id_list([]) == []


def test_none_entries_are_tolerated():
    assert _id_list([None, "ok-1"]) == ["ok-1"]  # type: ignore[list-item]


def test_list_is_capped():
    from services.user_profile import MAX_ID_ARRAY

    assert len(_id_list([f"id-{i}" for i in range(MAX_ID_ARRAY + 50)])) == MAX_ID_ARRAY


# ── profile helpers under hostile input ───────────────────────────────────


@pytest.mark.parametrize(
    "tool",
    ["mcp__", "mcp____x", "mcp", "", "mcp__x", "MCP__Postgres__query", "mcp__a__b__c"],
)
def test_mcp_server_name_never_raises(tool):
    result = _mcp_server_name(tool)
    assert result is None or isinstance(result, str)


def test_topics_tolerate_junk():
    topics = _topics_for(["", "\x00", "a" * 5000, "postgres"])
    assert "databases" in topics


# ── Active-user lookup (drives the nightly sweep's scope) ──────────────────


@pytest.mark.asyncio
async def test_active_users_returns_project_user_pairs(monkeypatch):
    rows = [
        {"project_id": "org-a", "user_id": "u1"},
        {"project_id": "org-a", "user_id": "u2"},
        {"project_id": "default", "user_id": "u3"},
    ]

    async def fake_query(sql: str, params: dict, **_kwargs):
        assert "session_stats_agg" in sql
        assert "since" in params
        return rows

    monkeypatch.setattr("services.telemetry.client.query", fake_query)

    active = await users_with_recent_activity()

    assert active == {("org-a", "u1"), ("org-a", "u2"), ("default", "u3")}


@pytest.mark.asyncio
async def test_active_users_returns_none_when_lookup_fails(monkeypatch):
    """None, not an empty set.

    An empty set means "nobody is active" and would skip every user; None
    tells the sweep to fall back to trying everyone. A telemetry outage must
    degrade the job, not silently switch it off.
    """

    from services.telemetry import TelemetryUnavailableError

    async def boom(sql: str, params: dict, **_kwargs):
        raise TelemetryUnavailableError("telemetry unreachable")

    monkeypatch.setattr("services.telemetry.client.query", boom)

    assert await users_with_recent_activity() is None


@pytest.mark.asyncio
async def test_active_users_empty_result_is_an_empty_set(monkeypatch):
    async def fake_query(sql: str, params: dict, **_kwargs):
        return []

    monkeypatch.setattr("services.telemetry.client.query", fake_query)

    assert await users_with_recent_activity() == set()
