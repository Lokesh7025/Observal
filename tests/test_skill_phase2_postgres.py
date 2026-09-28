# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Optional real-PostgreSQL row-contention test for skill review decisions.

Run with OBSERVAL_TEST_POSTGRES_URL pointing at a disposable test database.
Each test creates and removes its own schema; never point it at production.
"""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.routes import review, skill_files
from api.routes._skill_lock import lock_skill_version
from models.base import Base
from models.component_bundle import ComponentBundle
from models.mcp import ListingStatus
from models.skill import SkillListing, SkillVersion
from models.user import UserRole
from schemas.skill_resources import SkillFileOperations
from services.skill_revisions import skill_content_revision
from tests import discovery_support as ds

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def pg_store():
    url = os.environ.get("OBSERVAL_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set OBSERVAL_TEST_POSTGRES_URL to an isolated PostgreSQL test database")
    schema = f"phase2_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": f"{schema},public"}})
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[*ds.TABLES, ComponentBundle.__table__])
        yield async_sessionmaker(engine, expire_on_commit=False), engine
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def test_review_waits_for_writer_and_reloads_resource_bytes(pg_store, monkeypatch):
    maker, engine = pg_store
    async with maker() as db:
        admin = await ds.user(db, role=UserRole.admin)
        listing = await ds.skill(db, admin, status=ListingStatus.pending)
        await db.commit()
        listing_id = listing.id

    async with maker() as reviewer, maker() as author:
        stale = (await reviewer.execute(select(SkillListing).where(SkillListing.id == listing_id))).scalar_one()
        assert stale.latest_version.extra_files == []
        monkeypatch.setattr(review, "_find_listing", AsyncMock(return_value=("skill", stale)))
        await lock_skill_version(author, listing_id, stale.latest_version_id)
        version = (await author.execute(select(SkillVersion).where(SkillVersion.listing_id == listing_id))).scalar_one()
        version.extra_files = [{"path": "assets/data.txt", "content": "unreviewed"}]
        await author.flush()

        lock_attempted = asyncio.Event()

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def observe_lock(_conn, _cursor, statement, _params, _context, _many):
            if "skill_listings.latest_version_id" in statement and "FOR UPDATE" in statement.upper():
                lock_attempted.set()

        task = asyncio.create_task(review.approve(str(listing_id), reviewer, admin))
        try:
            await asyncio.wait_for(lock_attempted.wait(), timeout=5)
            await asyncio.sleep(0.15)
            assert not task.done(), "review must wait at the writer's listing lock"
            await author.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=5)
            assert exc.value.status_code == 409
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await author.rollback()
            await reviewer.rollback()

    async with maker() as db:
        row = (await db.execute(select(SkillVersion).where(SkillVersion.listing_id == listing_id))).scalar_one()
        assert row.status == ListingStatus.pending
        assert row.extra_files == [{"path": "assets/data.txt", "content": "unreviewed"}]


async def test_file_patch_rejects_concurrent_listing_rename(pg_store):
    maker, engine = pg_store
    async with maker() as db:
        owner = await ds.user(db)
        listing = await ds.skill(db, owner, status=ListingStatus.draft)
        await db.commit()
        listing_id, version_id = listing.id, listing.latest_version_id

    async with maker() as editor, maker() as writer:
        listing = (await editor.execute(select(SkillListing).where(SkillListing.id == listing_id))).scalar_one()
        observed = skill_content_revision(listing, listing.latest_version)
        await lock_skill_version(writer, listing_id, version_id)
        changed = (await writer.execute(select(SkillListing).where(SkillListing.id == listing_id))).scalar_one()
        changed.name = "Renamed while saving"
        await writer.flush()
        lock_attempted = asyncio.Event()

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def observe_lock(_conn, _cursor, statement, _params, _context, _many):
            if "skill_listings.latest_version_id" in statement and "FOR UPDATE" in statement.upper():
                lock_attempted.set()

        patch = SkillFileOperations.model_validate(
            {
                "observed_revision": observed,
                "operations": [{"action": "put", "file": {"path": "new.txt", "content": "new"}}],
            }
        )
        task = asyncio.create_task(
            skill_files.patch_skill_files(str(listing_id), version_id, patch, Response(), editor, owner)
        )
        try:
            await asyncio.wait_for(lock_attempted.wait(), timeout=5)
            await asyncio.sleep(0.15)
            assert not task.done(), "file save must wait for the listing rename"
            await writer.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=5)
            assert exc.value.status_code == 409
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await writer.rollback()
            await editor.rollback()
    async with maker() as db:
        changed = await db.get(SkillListing, listing_id)
        version = await db.get(SkillVersion, version_id)
        assert changed.name == "Renamed while saving"
        assert version.extra_files == []


async def test_file_patch_waits_for_writer_and_rejects_stale_revision(pg_store):
    maker, engine = pg_store
    async with maker() as db:
        owner = await ds.user(db)
        listing = await ds.skill(db, owner, status=ListingStatus.draft)
        await db.commit()
        listing_id, version_id = listing.id, listing.latest_version_id

    async with maker() as editor, maker() as writer:
        listing = (await editor.execute(select(SkillListing).where(SkillListing.id == listing_id))).scalar_one()
        observed = skill_content_revision(listing, listing.latest_version)
        await lock_skill_version(writer, listing_id, version_id)
        version = (await writer.execute(select(SkillVersion).where(SkillVersion.id == version_id))).scalar_one()
        version.extra_files = [{"path": "assets/data.txt", "content": "other edit"}]
        await writer.flush()
        lock_attempted = asyncio.Event()

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def observe_lock(_conn, _cursor, statement, _params, _context, _many):
            if "skill_listings.latest_version_id" in statement and "FOR UPDATE" in statement.upper():
                lock_attempted.set()

        patch = SkillFileOperations.model_validate(
            {
                "observed_revision": observed,
                "operations": [{"action": "put", "file": {"path": "new.txt", "content": "new"}}],
            }
        )
        task = asyncio.create_task(
            skill_files.patch_skill_files(str(listing_id), version_id, patch, Response(), editor, owner)
        )
        try:
            await asyncio.wait_for(lock_attempted.wait(), timeout=5)
            await asyncio.sleep(0.15)
            assert not task.done(), "file patch must wait at the writer's listing lock"
            await writer.commit()
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, timeout=5)
            assert exc.value.status_code == 409
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await writer.rollback()
            await editor.rollback()
    async with maker() as db:
        version = (await db.execute(select(SkillVersion).where(SkillVersion.id == version_id))).scalar_one()
        assert version.extra_files == [{"path": "assets/data.txt", "content": "other edit"}]
