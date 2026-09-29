# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Standalone installation chooses the persisted release before applying visibility/status gates."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from api.routes import skill
from models.mcp import ListingStatus
from models.skill import SkillDownload, SkillVersion
from schemas.skill import SkillInstallRequest
from tests import discovery_support as ds


@pytest.mark.asyncio
async def test_public_user_can_select_old_approved_release_behind_pending_pointer(monkeypatch):
    engine = ds.make_engine()
    maker = await ds.create_schema(engine)
    async with engine.begin() as conn:
        await conn.run_sync(SkillDownload.__table__.create)
    try:
        async with maker() as db:
            author = await ds.user(db)
            reader = await ds.user(db)
            listing = await ds.skill(db, author, content=None)
            approved_id = listing.latest_version_id
            candidate = await ds.add_skill_version(
                db,
                listing,
                author,
                version="2.0.0",
                status=ListingStatus.pending,
                content=None,
                description="Unreviewed private candidate notes",
            )
            await db.commit()
            listing_id, reader_id = listing.id, reader.id
            assert candidate.id != approved_id

        monkeypatch.setattr("api.routes.config.derive_endpoints", AsyncMock(return_value={"api": "https://api.test"}))
        selected_config = MagicMock(return_value={"skill": {"version": "1.2.0"}})
        monkeypatch.setattr("services.skill_config_generator.generate_skill_config", selected_config)
        async with maker() as db:
            reader = await db.get(type(reader), reader_id)
            detail = await skill.get_skill(str(listing_id), db, reader)
            assert detail.version == "1.2.0"
            assert detail.status == ListingStatus.approved
            assert "Unreviewed" not in detail.description
            response = await skill.install_skill(
                str(listing_id), SkillInstallRequest(harness="pi", version="1.2.0"), MagicMock(), db, reader
            )
            assert response.version_id == approved_id
            assert response.version == "1.2.0"
            assert selected_config.call_args.kwargs["version_override"].id == approved_id
            approved = (await db.execute(select(SkillVersion).where(SkillVersion.id == approved_id))).scalar_one()
            pending = (await db.execute(select(SkillVersion).where(SkillVersion.id == candidate.id))).scalar_one()
            assert approved.download_count == 1
            assert pending.download_count == 0

        async with maker() as db:
            reader = await db.get(type(reader), reader_id)
            default = await skill.install_skill(
                str(listing_id), SkillInstallRequest(harness="pi"), MagicMock(), db, reader
            )
            assert default.version_id == approved_id
            with pytest.raises(HTTPException) as refused:
                await skill.install_skill(
                    str(listing_id), SkillInstallRequest(harness="pi", version="2.0.0"), MagicMock(), db, reader
                )
            assert refused.value.status_code == 404
            approved = await db.get(SkillVersion, approved_id)
            pending = await db.get(SkillVersion, candidate.id)
            assert approved.download_count == 2
            assert pending.download_count == 0
    finally:
        await engine.dispose()
