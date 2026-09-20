# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""ClickHouse -> DuckDB cutover orchestration.

Switch-then-backfill: the running API already writes to the telemetry store.
This job exports the legacy ClickHouse tables into checksummed Parquet chunks,
imports them idempotently, rebuilds the derived tables, and verifies counts and
per-session content. Every step is resumable from ``cutover_state.json``.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger as optic

from observal_shared.migration.archive import read_manifest
from observal_shared.migration.connections import ChConnParams, TelemetryConnParams, connect_ch, connect_telemetry
from observal_shared.migration.exceptions import MigrationError, PrerequisiteError
from observal_shared.migration.legacy_clickhouse_export import _ch_query, export_ch, parse_clickhouse_url
from observal_shared.migration.telemetry_import import MANIFEST_FILENAME, import_telemetry
from observal_shared.telemetry_tables import IMPORTED_TABLES

if TYPE_CHECKING:
    from pathlib import Path

    from observal_shared.migration.progress import ProgressReporter

STATE_FILENAME = "cutover_state.json"
SPOT_CHECK_SESSIONS = 100
PHASES = ("preflight", "export", "import", "verify", "done")


@dataclass
class CutoverState:
    migration_id: str
    phase: str = "preflight"
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    export_completed_at: str | None = None
    import_completed_at: str | None = None
    verified_at: str | None = None
    source_counts: dict[str, int] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def load(cls, path: Path) -> CutoverState | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return cls(**data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


async def _ch_rows(params: ChConnParams, sql: str, client: httpx.AsyncClient, **extra) -> list[dict]:
    http_url, db, user, password = parse_clickhouse_url(params.url)
    resp = await _ch_query(http_url, db, user, password, sql, http_client=client, extra_params=extra or None)
    return resp.json().get("data", [])


async def _source_counts(params: ChConnParams) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        existing = {
            r["name"]
            for r in await _ch_rows(
                params, "SELECT name FROM system.tables WHERE database = currentDatabase() FORMAT JSON", client
            )
        }
        for table in IMPORTED_TABLES:
            if table not in existing:
                counts[table] = 0
                continue
            final = " FINAL" if table in {"session_events", "layer_snapshots"} else ""
            rows = await _ch_rows(params, f"SELECT count() AS c FROM {table}{final} FORMAT JSON", client)
            counts[table] = int(rows[0]["c"]) if rows else 0
    return counts


async def _target_counts(params: TelemetryConnParams, cutoff: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        for table in IMPORTED_TABLES:
            version_col = {"session_events": "ingested_at", "layer_snapshots": "uploaded_at"}.get(table)
            where = ""
            bound: dict[str, Any] = {}
            if cutoff and version_col:
                where = f' WHERE "{version_col}" <= CAST($cutoff AS TIMESTAMP)'
                bound["cutoff"] = cutoff
            resp = await client.post(
                f"{params.base_url}/v1/query",
                json={"sql": f'SELECT count(*) AS c FROM "{table}"{where}', "params": bound},
                headers=params.headers,
            )
            resp.raise_for_status()
            rows = resp.json()["rows"]
            counts[table] = int(rows[0]["c"]) if rows else 0
    return counts


def _session_digest(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: int(r["line_offset"])):
        digest.update(f"{row['line_offset']}|{row.get('line_hash', '')}|{row.get('event_type', '')}\n".encode())
    return digest.hexdigest()


async def _spot_check(ch: ChConnParams, tel: TelemetryConnParams, sample: int) -> dict[str, Any]:
    """Compare a random sample of sessions event-by-event (line_offset, line_hash, event_type)."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        ids = await _ch_rows(
            ch,
            "SELECT DISTINCT project_id, user_id, harness, session_id FROM session_events FINAL LIMIT 20000 FORMAT JSON",
            client,
        )
        if not ids:
            return {"sampled": 0, "mismatched": []}
        random.shuffle(ids)
        mismatched: list[str] = []
        checked = 0
        for ident in ids[:sample]:
            src = await _ch_rows(
                ch,
                "SELECT line_offset, line_hash, event_type FROM session_events FINAL "
                "WHERE project_id = {pid:String} AND user_id = {uid:String} AND harness = {h:String} "
                "AND session_id = {sid:String} AND is_source_record = 1 FORMAT JSON",
                client,
                param_pid=ident["project_id"],
                param_uid=ident["user_id"],
                param_h=ident["harness"],
                param_sid=ident["session_id"],
            )
            from observal_shared.telemetry_keys import session_key

            key = session_key(ident["project_id"], ident["user_id"], ident["harness"], ident["session_id"])
            resp = await client.post(
                f"{tel.base_url}/v1/query",
                json={
                    "sql": "SELECT line_offset, line_hash, event_type FROM session_events "
                    "WHERE session_key = $key AND is_source_record",
                    "params": {"key": key},
                },
                headers=tel.headers,
            )
            resp.raise_for_status()
            dst = resp.json()["rows"]
            checked += 1
            if _session_digest(src) != _session_digest(dst):
                mismatched.append(ident["session_id"])
        return {"sampled": checked, "mismatched": mismatched}


async def run_cutover(
    ch_params: ChConnParams,
    telemetry_params: TelemetryConnParams,
    artifact_dir: Path,
    reporter: ProgressReporter,
    *,
    resume: bool = False,
    verify_only: bool = False,
    spot_check_sessions: int = SPOT_CHECK_SESSIONS,
) -> CutoverState:
    """Run (or resume) the cutover. Never deletes anything on either side."""
    state_path = artifact_dir / STATE_FILENAME
    state = CutoverState.load(state_path) if (resume or verify_only) else None
    if state is None:
        if artifact_dir.exists() and any(artifact_dir.iterdir()):
            raise PrerequisiteError(f"artifact directory is not empty (use --resume): {artifact_dir}")
        state = CutoverState(migration_id=f"cutover-{uuid.uuid4().hex[:12]}")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        state.save(state_path)
    chunk_dir = artifact_dir / "chunks"

    try:
        # ── Preflight ───────────────────────────────────────────
        await reporter.update(phase="preflight", pct=0, message="Checking ClickHouse and the telemetry store")
        await connect_ch(ch_params)
        health = await connect_telemetry(telemetry_params)
        if health.get("writer_paused"):
            raise PrerequisiteError("telemetry writer is paused; resume writes before running the cutover")
        if not state.source_counts:
            state.source_counts = await _source_counts(ch_params)
            state.save(state_path)
        free = shutil.disk_usage(artifact_dir).free
        optic.info("cutover {}: source counts {} free disk {} MiB", state.migration_id, state.source_counts, free >> 20)

        if not verify_only:
            # ── Export ──────────────────────────────────────────
            if state.export_completed_at is None:
                state.phase = "export"
                state.save(state_path)
                if chunk_dir.exists() and not (chunk_dir / MANIFEST_FILENAME).exists():
                    shutil.rmtree(chunk_dir, ignore_errors=True)
                if not (chunk_dir / MANIFEST_FILENAME).exists():
                    await export_ch(ch_params, None, chunk_dir, reporter, migration_id=state.migration_id)
                state.export_completed_at = datetime.now(UTC).isoformat()
                state.save(state_path)

            # ── Import + rebuild ────────────────────────────────
            if state.import_completed_at is None:
                state.phase = "import"
                state.save(state_path)
                await import_telemetry(telemetry_params, chunk_dir, reporter, rebuild=True)
                state.import_completed_at = datetime.now(UTC).isoformat()
                state.save(state_path)

        # ── Verify ──────────────────────────────────────────────
        state.phase = "verify"
        state.save(state_path)
        await reporter.update(phase="verify", pct=0, message="Comparing row counts")
        manifest = read_manifest(chunk_dir / MANIFEST_FILENAME) if (chunk_dir / MANIFEST_FILENAME).exists() else {}
        cutoff = manifest.get("export_time_cutoff")
        target = await _target_counts(telemetry_params, cutoff)
        count_mismatches = {
            t: {"source": state.source_counts.get(t, 0), "target": target.get(t, 0)}
            for t in IMPORTED_TABLES
            if target.get(t, 0) < state.source_counts.get(t, 0)
        }
        await reporter.update(phase="verify", pct=50, message=f"Spot-checking {spot_check_sessions} sessions")
        spot = await _spot_check(ch_params, telemetry_params, spot_check_sessions)
        state.verification = {
            "source_counts": state.source_counts,
            "target_counts": target,
            "count_mismatches": count_mismatches,
            "spot_check": spot,
            "export_time_cutoff": cutoff,
        }
        ok = not count_mismatches and not spot["mismatched"]
        if not ok:
            state.phase = "verify_failed"
            state.error = f"count mismatches: {count_mismatches}; session mismatches: {spot['mismatched'][:10]}"
            state.save(state_path)
            raise MigrationError(f"cutover verification failed: {state.error}")
        state.verified_at = datetime.now(UTC).isoformat()
        state.phase = "done"
        state.error = None
        state.save(state_path)
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(
                f"{telemetry_params.base_url}/v1/write/backfill-state",
                json={"migration_id": state.migration_id, "phase": "done", "pct": 100, "message": "backfill verified"},
                headers=telemetry_params.headers,
            )
        await reporter.update(phase="done", pct=100, message="Cutover complete and verified")
        return state
    except Exception as exc:
        if state.phase != "verify_failed":
            state.error = f"{type(exc).__name__}: {exc}"
            state.save(state_path)
        raise


async def reverse_cutover(
    telemetry_params: TelemetryConnParams,
    ch_params: ChConnParams,
    artifact_dir: Path,
    reporter: ProgressReporter,
    *,
    since: str | None,
) -> dict[str, Any]:
    """Export DuckDB rows (optionally only those ingested after *since*) so they can be loaded back into ClickHouse.

    Loading into ClickHouse is left to ``clickhouse-client`` /
    ``INSERT ... FORMAT Parquet`` on the operator's side; this function only
    produces the verified Parquet artifacts and a manifest.
    """
    from observal_shared.migration.telemetry_export import export_telemetry

    await connect_ch(ch_params)
    migration_id = f"reverse-{uuid.uuid4().hex[:12]}"
    result = await export_telemetry(
        telemetry_params, artifact_dir / "reverse", reporter, migration_id=migration_id, since=since
    )
    return {"migration_id": migration_id, "output_dir": result.output_dir, "rows": result.total_rows}


__all__ = ["PHASES", "STATE_FILENAME", "CutoverState", "reverse_cutover", "run_cutover"]
