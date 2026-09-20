-- SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
-- SPDX-License-Identifier: Apache-2.0
--
-- DuckDB telemetry baseline. This is the only schema file: no constraints,
-- no indexes. Logical keys are enforced by the single writer.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    VARCHAR,
    name       VARCHAR,
    applied_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS session_events (
    session_key         BIGINT,
    parent_session_key  BIGINT,
    session_id          VARCHAR,
    project_id          VARCHAR,
    user_id             VARCHAR,
    harness             VARCHAR,
    agent_id            VARCHAR,
    agent_version       VARCHAR,
    layer_hash          VARCHAR,
    line_offset         UBIGINT,
    source_end_offset   UBIGINT DEFAULT 0,
    line_hash           VARCHAR DEFAULT '',
    source_sha256       VARCHAR DEFAULT '',
    is_source_record    BOOLEAN DEFAULT true,
    rendered            BOOLEAN DEFAULT true,
    event_type          VARCHAR,
    "timestamp"         TIMESTAMP,
    uuid                VARCHAR,
    parent_uuid         VARCHAR,
    tool_name           VARCHAR,
    tool_id             VARCHAR,
    content_preview     VARCHAR DEFAULT '',
    content_length      UINTEGER DEFAULT 0,
    raw_line            VARCHAR DEFAULT '',
    raw_line_truncated  UTINYINT DEFAULT 0,
    ingested_at         TIMESTAMP DEFAULT current_timestamp,
    credits             DOUBLE DEFAULT 0,
    parent_session_id   VARCHAR,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_write_tokens  INTEGER DEFAULT 0,
    model               VARCHAR DEFAULT ''
);

CREATE TABLE IF NOT EXISTS session_checkpoints (
    session_key         BIGINT,
    project_id          VARCHAR,
    user_id             VARCHAR,
    harness             VARCHAR,
    session_id          VARCHAR,
    acknowledged_line   BIGINT,
    acknowledged_offset UBIGINT DEFAULT 0,
    checkpoint_version  UBIGINT,
    updated_at          TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS session_stats_agg (
    session_key         BIGINT,
    project_id          VARCHAR,
    session_id          VARCHAR,
    user_id             VARCHAR,
    harness             VARCHAR,
    agent_id            VARCHAR DEFAULT '',
    agent_version       VARCHAR DEFAULT '',
    parent_session_id   VARCHAR DEFAULT '',
    layer_hash          VARCHAR DEFAULT '',
    first_event_time    TIMESTAMP,
    last_event_time     TIMESTAMP,
    event_count         BIGINT DEFAULT 0,
    prompt_count        BIGINT DEFAULT 0,
    tool_call_count     BIGINT DEFAULT 0,
    tool_result_count   BIGINT DEFAULT 0,
    input_tokens        BIGINT DEFAULT 0,
    output_tokens       BIGINT DEFAULT 0,
    cache_read_tokens   BIGINT DEFAULT 0,
    cache_write_tokens  BIGINT DEFAULT 0,
    total_credits       DOUBLE DEFAULT 0,
    model               VARCHAR DEFAULT '',
    summary_version     UBIGINT,
    updated_at          TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS layer_snapshots (
    snapshot_key        BIGINT,
    hash                VARCHAR,
    project_id          VARCHAR,
    user_id             VARCHAR,
    harness             VARCHAR,
    content             VARCHAR,
    uploaded_at         TIMESTAMP DEFAULT current_timestamp,
    file_count          USMALLINT DEFAULT 0,
    total_size          UINTEGER DEFAULT 0,
    lockfile_hash       VARCHAR DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    event_id        UUID,
    "timestamp"     TIMESTAMP,
    actor_id        VARCHAR DEFAULT '',
    actor_email     VARCHAR DEFAULT '',
    actor_role      VARCHAR DEFAULT '',
    action          VARCHAR DEFAULT '',
    resource_type   VARCHAR DEFAULT '',
    resource_id     VARCHAR DEFAULT '',
    resource_name   VARCHAR DEFAULT '',
    http_method     VARCHAR DEFAULT '',
    http_path       VARCHAR DEFAULT '',
    status_code     USMALLINT DEFAULT 0,
    ip_address      VARCHAR DEFAULT '',
    user_agent      VARCHAR DEFAULT '',
    detail          VARCHAR DEFAULT '',
    sensitivity     VARCHAR DEFAULT 'standard',
    request_id      VARCHAR DEFAULT '',
    outcome         VARCHAR DEFAULT '',
    duration_ms     FLOAT DEFAULT 0,
    chain_hash      VARCHAR DEFAULT '',
    source          VARCHAR DEFAULT 'server'
);

CREATE TABLE IF NOT EXISTS security_events (
    event_id        UUID,
    "timestamp"     TIMESTAMP,
    event_type      VARCHAR,
    severity        VARCHAR,
    actor_id        VARCHAR DEFAULT '',
    actor_email     VARCHAR DEFAULT '',
    actor_role      VARCHAR DEFAULT '',
    target_id       VARCHAR DEFAULT '',
    target_type     VARCHAR DEFAULT '',
    outcome         VARCHAR DEFAULT '',
    source_ip       VARCHAR DEFAULT '',
    user_agent      VARCHAR DEFAULT '',
    detail          VARCHAR DEFAULT ''
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id     UUID,
    event_id        UUID,
    alert_rule_id   UUID,
    attempt_number  UTINYINT,
    "timestamp"     TIMESTAMP,
    webhook_url     VARCHAR,
    status_code     USMALLINT,
    delivery_status VARCHAR,
    error           VARCHAR,
    duration_ms     FLOAT,
    payload_size    UINTEGER
);

CREATE TABLE IF NOT EXISTS telemetry_import_ledger (
    migration_id    VARCHAR,
    chunk_id        VARCHAR,
    "table"         VARCHAR,
    sha256          VARCHAR,
    row_count       BIGINT,
    applied_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS telemetry_backfill_state (
    migration_id    VARCHAR,
    phase           VARCHAR,
    pct             SMALLINT,
    message         VARCHAR,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);
