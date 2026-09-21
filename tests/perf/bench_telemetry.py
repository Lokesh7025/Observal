# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Scale benchmark for the telemetry store (acceptance A-20).

Generates a synthetic session_events table straight into a DuckDB file using
the store's schema, then times the hot query paths through the real service
(session detail by key, session list, exec-dashboard aggregates, dedup lookup,
ingest write, backup). Not part of the default test run::

    uv run --project observal-server python tests/perf/bench_telemetry.py --rows 30000000

The 30M-row target from the plan needs ~40 GB of disk and ~20 minutes to
generate; ``--rows 3000000`` gives a representative 10% sample in ~2 minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

import duckdb
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "observal-server"))
sys.path.insert(0, str(ROOT / "packages" / "observal-shared"))

from telemetry_store.api import create_app  # noqa: E402
from telemetry_store.db import Database  # noqa: E402
from telemetry_store.settings import TelemetrySettings  # noqa: E402

TOKEN = "bench"


def generate(db_path: Path, rows: int, sessions: int) -> None:
    """Populate session_events / session_stats_agg with a realistic shape using pure SQL."""
    settings = TelemetrySettings(
        db_path=db_path,
        temp_dir=db_path.parent / "tmp",
        export_dir=db_path.parent / "exports",
        token=TOKEN,
        memory_limit="1536MB",
        threads=4,
        read_threads=4,
        read_queue_max=64,
        query_timeout_ms=600_000,
        write_queue_timeout_ms=10_000,
        max_result_rows=1_000_000,
        checkpoint_interval_s=300,
        checkpoint_threshold="256MB",
        bind_host="127.0.0.1",
        bind_port=0,
        metrics_public=True,
        allow_anonymous=False,
    )
    db = Database(settings)
    conn = db.open()
    db.apply_schema()
    per_session = max(1, rows // sessions)
    t0 = time.perf_counter()
    conn.execute(
        f"""
        INSERT INTO session_events (
            session_key, parent_session_key, session_id, project_id, user_id, harness, agent_id, agent_version,
            line_offset, source_end_offset, line_hash, source_sha256, is_source_record, rendered, event_type,
            "timestamp", uuid, content_preview, content_length, raw_line, ingested_at, credits,
            input_tokens, output_tokens, model)
        SELECT
            (hash(s.i) % 9223372036854775807)::BIGINT     AS session_key,
            NULL                                            AS parent_session_key,
            'sess-' || s.i                                  AS session_id,
            'default'                                       AS project_id,
            'user-' || (s.i % 500)                          AS user_id,
            CASE s.i % 4 WHEN 0 THEN 'claude-code' WHEN 1 THEN 'kiro' WHEN 2 THEN 'cursor' ELSE 'pi' END AS harness,
            CASE WHEN s.i % 3 = 0 THEN 'agent-' || (s.i % 40) ELSE NULL END AS agent_id,
            '1.0.0'                                         AS agent_version,
            l.j                                             AS line_offset,
            (l.j + 1) * 2048                                AS source_end_offset,
            md5(s.i::VARCHAR || ':' || l.j::VARCHAR)        AS line_hash,
            md5('sha' || s.i::VARCHAR || ':' || l.j::VARCHAR) AS source_sha256,
            true, true,
            CASE l.j % 3 WHEN 0 THEN 'user_prompt' WHEN 1 THEN 'tool_call' ELSE 'tool_result' END AS event_type,
            TIMESTAMP '2026-01-01 00:00:00' + to_seconds(s.i * 37 + l.j) AS "timestamp",
            'uuid-' || s.i || '-' || l.j,
            'preview text for line ' || l.j,
            2048,
            repeat('{{"type":"assistant","message":{{"content":"synthetic transcript line"}}}}', 25) AS raw_line,
            TIMESTAMP '2026-01-01 00:00:00' + to_seconds(s.i * 37 + l.j),
            0.0,
            CASE WHEN l.j % 3 = 1 THEN 300 ELSE 0 END,
            CASE WHEN l.j % 3 = 1 THEN 120 ELSE 0 END,
            'claude-x'
        FROM range({sessions}) s(i), range({per_session}) l(j)
        """
    )
    print(f"generated {rows:,} events in {time.perf_counter() - t0:.1f}s", flush=True)
    t0 = time.perf_counter()
    from telemetry_store.sql import SUMMARY_COLUMNS, SUMMARY_SELECT

    conn.execute(
        f"INSERT INTO session_stats_agg ({SUMMARY_COLUMNS}) "
        + SUMMARY_SELECT.replace("WHERE session_key IN (SELECT unnest($keys::BIGINT[]))", "").replace(
            "$version::UBIGINT", "1::UBIGINT"
        ),
    )
    conn.execute("CHECKPOINT")
    print(f"built {sessions:,} summaries in {time.perf_counter() - t0:.1f}s", flush=True)
    db.close()


async def bench(db_path: Path, sessions: int) -> dict:
    settings = TelemetrySettings(
        db_path=db_path,
        temp_dir=db_path.parent / "tmp",
        export_dir=db_path.parent / "exports",
        token=TOKEN,
        memory_limit="1536MB",
        threads=4,
        read_threads=4,
        read_queue_max=64,
        query_timeout_ms=60_000,
        write_queue_timeout_ms=10_000,
        max_result_rows=200_000,
        checkpoint_interval_s=300,
        checkpoint_threshold="256MB",
        bind_host="127.0.0.1",
        bind_port=0,
        metrics_public=True,
        allow_anonymous=False,
    )
    app = create_app(settings)
    results: dict[str, dict] = {}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://b", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=120
        ) as c:

            async def timed(name: str, coro_factory, n: int = 7):
                samples = []
                for _ in range(n):
                    t0 = time.perf_counter()
                    r = await coro_factory()
                    assert r.status_code == 200, (name, r.text[:200])
                    samples.append((time.perf_counter() - t0) * 1000)
                samples.sort()
                results[name] = {
                    "p50_ms": round(statistics.median(samples), 1),
                    "p95_ms": round(samples[int(0.95 * (n - 1))], 1),
                }
                print(
                    f"{name:<40} p50={results[name]['p50_ms']:>8.1f}ms  p95={results[name]['p95_ms']:>8.1f}ms",
                    flush=True,
                )

            def q(sql, **params):
                return lambda: c.post("/v1/query", json={"sql": sql, "params": params})

            key = (
                duckdb.connect().execute(f"SELECT (hash({sessions // 2}) % 9223372036854775807)::BIGINT").fetchone()[0]
            )
            await timed(
                "session detail (by key, ~100 rows)",
                q(
                    "SELECT line_offset, timestamp, event_type, content_preview, tool_name, uuid, raw_line FROM session_events "
                    "WHERE session_key = $key AND session_id = $sid AND rendered ORDER BY line_offset",
                    key=key,
                    sid=f"sess-{sessions // 2}",
                ),
            )
            await timed(
                "dedup lookup (line range)",
                q(
                    "SELECT line_offset, line_hash FROM session_events WHERE session_key = $key AND session_id = $sid "
                    "AND is_source_record AND line_offset::BIGINT BETWEEN 0 AND 999",
                    key=key,
                    sid=f"sess-{sessions // 2}",
                ),
            )
            await timed(
                "checkpoint lookup",
                q("SELECT acknowledged_line FROM session_checkpoints WHERE session_key = $key", key=key),
            )
            await timed(
                "session list (page of 50)",
                q(
                    "SELECT session_id, first_event_time, last_event_time, prompt_count, tool_result_count, input_tokens, output_tokens, model, harness, agent_id, user_id "
                    "FROM session_stats_agg WHERE session_id <> '' AND parent_session_id = '' AND prompt_count > 0 AND user_id = $uid "
                    "ORDER BY last_event_time DESC LIMIT 50 OFFSET 0",
                    uid="user-7",
                ),
            )
            await timed(
                "overview: tool calls last 400d",
                q(
                    "SELECT sum(tool_call_count) AS c FROM session_stats_agg WHERE last_event_time > now()::TIMESTAMP - to_days(400)"
                ),
            )
            await timed(
                "exec: sessions by month",
                q(
                    "SELECT date_trunc('month', first_event_time) AS month, count(*) AS c FROM session_stats_agg GROUP BY month ORDER BY month"
                ),
            )
            await timed(
                "exec: tokens by user (30d window)",
                q(
                    "SELECT user_id, count(*) AS sessions, sum(input_tokens + output_tokens) AS tokens FROM session_stats_agg "
                    "WHERE first_event_time >= TIMESTAMP '2026-01-01' GROUP BY user_id ORDER BY tokens DESC LIMIT 25"
                ),
            )
            await timed(
                "audit-style scan: tool usage (events)",
                q(
                    "SELECT tool_name, count(*) AS calls FROM session_events WHERE event_type = 'tool_call' GROUP BY tool_name ORDER BY calls DESC LIMIT 10"
                ),
                n=3,
            )

            events = [
                {
                    "session_id": "bench-live",
                    "project_id": "default",
                    "user_id": "user-1",
                    "harness": "claude-code",
                    "line_offset": i,
                    "line_hash": f"h{i}",
                    "source_sha256": f"s{i}",
                    "event_type": "user_prompt",
                    "timestamp": "2026-06-01 00:00:00.000",
                    "raw_line": "x" * 2048,
                    "content_preview": "p",
                    "input_tokens": 1,
                }
                for i in range(1000)
            ]
            identity = {
                "project_id": "default",
                "user_id": "user-1",
                "harness": "claude-code",
                "session_id": "bench-live",
            }
            await timed(
                "ingest write (1000 rows + summary + ckpt)",
                lambda: c.post("/v1/write/session-batch", json={"events": events, "advance_checkpoint": identity}),
                n=5,
            )

            t0 = time.perf_counter()
            r = await c.post("/v1/admin/backup")
            job = r.json()["id"]
            while True:
                j = (await c.get(f"/v1/jobs/{job}")).json()
                if j["state"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.5)
            results["backup"] = {
                "seconds": round(time.perf_counter() - t0, 1),
                "bytes": j.get("result", {}).get("bytes"),
                "state": j["state"],
            }
            print(f"{'backup (COPY FROM DATABASE)':<40} {results['backup']}", flush=True)
            results["health"] = (await c.get("/v1/health")).json()
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=3_000_000)
    parser.add_argument("--sessions", type=int, default=None)
    parser.add_argument("--dir", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    sessions = args.sessions or max(1, args.rows // 100)
    workdir = args.dir or Path(tempfile.mkdtemp(prefix="telemetry-bench-"))
    db_path = workdir / "bench.duckdb"
    print(f"workdir={workdir} rows={args.rows:,} sessions={sessions:,}", flush=True)
    if not db_path.exists():
        generate(db_path, args.rows, sessions)
    print(f"db file: {db_path.stat().st_size / 1e9:.2f} GB", flush=True)
    results = asyncio.run(bench(db_path, sessions))
    results["rows"] = args.rows
    results["sessions"] = sessions
    results["file_gb"] = round(db_path.stat().st_size / 1e9, 2)
    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
