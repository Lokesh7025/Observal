<!--
SPDX-FileCopyrightText: 2026 Hari Srinivasan <harisrini21@gmail.com>
SPDX-License-Identifier: Apache-2.0
-->

# Resource Tuning

Connection pool sizes, query limits, and timeout configuration. These settings control how Observal connects to its backing stores (PostgreSQL, Redis, the telemetry store). Most deployments work fine with defaults. Tune when you see connection timeouts, pool exhaustion, or slow queries under load.

## When to Tune

- **Connection pool errors** in API logs ("pool exhausted", "connection timeout")
- **Slow dashboard loads** under concurrent users (increase pool sizes)
- **OOM kills** on the API container (decrease pool sizes, each connection uses memory)
- **Telemetry query timeouts** (504 `telemetry_timeout`) on wide dashboard ranges (raise `TELEMETRY_QUERY_TIMEOUT_MS` on the store or `TELEMETRY_TIMEOUT` on the API)

## PostgreSQL {#postgresql}

### DB Pool Size {#db-pool-size}

Number of persistent database connections maintained in the connection pool.

| Value | Effect |
|-------|--------|
| `10` (default) | Suitable for small teams (< 20 concurrent users) |
| `20` | Medium deployments (20-100 users) |
| `50` | Large deployments (100+ concurrent users) |

**Memory impact:** Each connection uses approximately 5MB of RAM on the API server.

**When to increase:** You see "pool exhausted" errors or requests queuing during peak usage.

**When to decrease:** Running on memory-constrained containers, or your PostgreSQL instance has a low `max_connections` limit.

### DB Max Overflow {#db-max-overflow}

Temporary connections created when the pool is full. These are closed after use.

| Value | Effect |
|-------|--------|
| `20` (default) | Allows bursts of up to 30 total connections (pool + overflow) |
| `0` | No overflow; requests wait for a pool connection (safest for DB) |
| `50` | High burst tolerance; use when traffic is very spiky |

**Total max connections** = pool_size + max_overflow. Ensure your PostgreSQL `max_connections` is at least this value plus a buffer for admin connections.

## Redis {#redis}

### Redis Max Connections {#redis-max-connections}

Maximum concurrent connections to Redis.

| Value | Effect |
|-------|--------|
| `50` (default) | Handles most workloads |
| `100` | High-traffic deployments with heavy pub/sub (GraphQL subscriptions) |
| `20` | Constrained environments with limited Redis resources |

**When to increase:** "Connection pool exhausted" errors in Redis client logs, or high latency on GraphQL subscriptions.

### Redis Timeout {#redis-timeout}

Socket timeout in seconds for Redis operations.

| Value | Effect |
|-------|--------|
| `2.0` (default) | Balanced; detects failures quickly without false positives |
| `5.0` | Use when Redis is on a high-latency network (cross-region) |
| `1.0` | Aggressive; faster failure detection but may false-positive on slow queries |

**When to increase:** Redis is in a different availability zone or region, causing occasional timeout errors on valid operations.

## Telemetry store {#telemetry}

The telemetry store is tuned through environment variables on the `observal-telemetry` container and on the API, not through dynamic settings. See [Telemetry service](telemetry-service.md#configuration) for the full table.

| Variable (where) | Default | Tune when |
|------|--------|--------|
| `TELEMETRY_MEMORY_LIMIT` (store) | `1536MB` | Aggregations spill to disk or the container is OOM-killed; keep ~25% below the container limit |
| `TELEMETRY_THREADS` (store) | `4` | Match the vCPUs you can dedicate; DuckDB scans scale with threads |
| `TELEMETRY_READ_THREADS` / `TELEMETRY_READ_QUEUE_MAX` (store) | `4` / `64` | API logs `429 telemetry_busy` under dashboard load |
| `TELEMETRY_QUERY_TIMEOUT_MS` (store) | `30000` | Dashboards return `504 telemetry_timeout` on wide ranges |
| `TELEMETRY_TIMEOUT` / `TELEMETRY_WRITE_TIMEOUT` (API) | `30` / `60` s | Must exceed the store-side timeout; ingest writes of 1 000 lines take ~250 ms at 30 M rows |
| `TELEMETRY_MAX_CONNECTIONS` (API) | `50` | Many API workers behind one store |

### Skip DDL on Startup {#skip-ddl-on-startup}

Skip PostgreSQL `create_all` on server startup. The telemetry store applies its own schema when it starts and is not affected by this setting.

| Value | Effect |
|-------|--------|
| `false` (default) | Base schema creation runs automatically on every startup |
| `true` | Skip DDL when the init container has already applied migrations (recommended for multiple API replicas) |
