# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Canonical SQL used by both the live writer and the rebuild job.

There is exactly one definition of the session summary and of the contiguous
checkpoint so live ingest and post-migration rebuilds cannot drift.
"""

from __future__ import annotations

#: Sentinel bounds used by the ClickHouse implementation; preserved verbatim.
TS_LOWER = "1971-01-01 00:00:00"
TS_UPPER = "2099-01-01 00:00:00"


def sql_string_literal(value: str) -> str:
    """Quote a trusted value for statements where DuckDB cannot bind parameters."""
    if "\x00" in value:
        raise ValueError("SQL string literal contains a NUL byte")
    return "'" + value.replace("'", "''") + "'"


_VALID_TS = f"rendered AND \"timestamp\" > TIMESTAMP '{TS_LOWER}' AND \"timestamp\" < TIMESTAMP '{TS_UPPER}'"

#: Aggregate one or more sessions from ``session_events``. Bind ``$keys`` (BIGINT list)
#: and ``$version`` (UBIGINT). The caller wraps it in ``INSERT INTO session_stats_agg (...)``.
SUMMARY_SELECT = f"""
SELECT
    session_key,
    project_id,
    session_id,
    user_id,
    harness,
    coalesce(any_value(agent_id) FILTER (WHERE agent_id IS NOT NULL AND agent_id <> ''), '')            AS agent_id,
    coalesce(any_value(agent_version) FILTER (WHERE agent_version IS NOT NULL AND agent_version <> ''), '') AS agent_version,
    coalesce(any_value(parent_session_id) FILTER (WHERE parent_session_id IS NOT NULL), '')             AS parent_session_id,
    coalesce(any_value(layer_hash) FILTER (WHERE layer_hash IS NOT NULL AND layer_hash <> ''), '')      AS layer_hash,
    min("timestamp") FILTER (WHERE {_VALID_TS})                                                          AS first_event_time,
    max("timestamp") FILTER (WHERE {_VALID_TS})                                                          AS last_event_time,
    count(*) FILTER (WHERE rendered)                                                                     AS event_count,
    count(*) FILTER (WHERE rendered AND event_type = 'user_prompt')                                      AS prompt_count,
    count(*) FILTER (WHERE rendered AND event_type = 'tool_call')                                        AS tool_call_count,
    count(*) FILTER (WHERE rendered AND event_type = 'tool_result')                                      AS tool_result_count,
    coalesce(sum(input_tokens) FILTER (WHERE rendered), 0)                                               AS input_tokens,
    coalesce(sum(output_tokens) FILTER (WHERE rendered), 0)                                              AS output_tokens,
    coalesce(sum(cache_read_tokens) FILTER (WHERE rendered), 0)                                          AS cache_read_tokens,
    coalesce(sum(cache_write_tokens) FILTER (WHERE rendered), 0)                                         AS cache_write_tokens,
    coalesce(max(credits), 0)                                                                            AS total_credits,
    coalesce(arg_max(model, line_offset) FILTER (WHERE rendered AND model <> ''), '')                    AS model,
    $version::UBIGINT                                                                                    AS summary_version,
    current_timestamp::TIMESTAMP                                                                         AS updated_at
FROM session_events
WHERE session_key IN (SELECT unnest($keys::BIGINT[]))
GROUP BY session_key, project_id, session_id, user_id, harness
"""

SUMMARY_COLUMNS = (
    "session_key, project_id, session_id, user_id, harness, agent_id, agent_version, parent_session_id, "
    "layer_hash, first_event_time, last_event_time, event_count, prompt_count, tool_call_count, "
    "tool_result_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_credits, "
    "model, summary_version, updated_at"
)

#: Highest contiguous source line after ``$ack`` for ``$key``. Returns one row
#: ``(acknowledged_line, acknowledged_offset)`` or NULLs when nothing advances.
CONTIGUOUS_CHECKPOINT = """
WITH src AS (
    SELECT DISTINCT line_offset::BIGINT AS line_offset, source_end_offset
    FROM session_events
    WHERE session_key = $key AND is_source_record AND line_offset::BIGINT > $ack
),
numbered AS (
    SELECT line_offset, source_end_offset,
           row_number() OVER (ORDER BY line_offset) AS rn
    FROM src
)
SELECT max(line_offset) AS acknowledged_line,
       arg_max(source_end_offset, line_offset) AS acknowledged_offset
FROM numbered
WHERE line_offset = $ack + rn
"""

#: Batch variant for rebuilds: contiguous prefix from -1 for many sessions at once.
CONTIGUOUS_CHECKPOINT_BATCH = """
WITH src AS (
    SELECT DISTINCT session_key, project_id, user_id, harness, session_id,
           line_offset::BIGINT AS line_offset, source_end_offset
    FROM session_events
    WHERE session_key IN (SELECT unnest($keys::BIGINT[])) AND is_source_record
),
numbered AS (
    SELECT *, row_number() OVER (PARTITION BY session_key ORDER BY line_offset) AS rn FROM src
)
SELECT session_key,
       any_value(project_id) AS project_id,
       any_value(user_id) AS user_id,
       any_value(harness) AS harness,
       any_value(session_id) AS session_id,
       max(line_offset) AS acknowledged_line,
       arg_max(source_end_offset, line_offset) AS acknowledged_offset
FROM numbered
WHERE line_offset = rn - 1
GROUP BY session_key
"""
