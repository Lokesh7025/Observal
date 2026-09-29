# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""A saved candidate owns new bytes without moving the approved pointer."""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import select

from api.routes import component_versions, skill_files
from models.inbox import InboxItem, InboxItemEvent
from models.mcp import ListingStatus
from models.skill import SkillListing, SkillVersion
from models.user import User, UserRole
from schemas.skill import SkillCandidateDraftRequest, SkillUpdateRequest
from schemas.skill_resources import SkillFileOperations, SkillVersionRevisionRequest
from services.skill_revisions import skill_content_revision
from tests import discovery_support as ds

FIXTURE = json.loads((Path(__file__).parent / "fixtures/skill_folder_contract.json").read_text())


@pytest.mark.asyncio
async def test_rejected_git_version_is_editable_and_resubmittable_behind_approved_pointer():
    engine = ds.make_engine()
    maker = await ds.create_schema(engine)
    async with engine.begin() as conn:
        await conn.run_sync(InboxItem.__table__.create)
        await conn.run_sync(InboxItemEvent.__table__.create)
    try:
        async with maker() as db:
            owner = await ds.user(db)
            await ds.user(db, role=UserRole.reviewer)
            listing = await ds.skill(db, owner, status=ListingStatus.approved)
            approved_id = listing.latest_version_id
            rejected = await ds.add_skill_version(
                db, listing, owner, version="1.3.0", status=ListingStatus.rejected, set_latest=False
            )
            rejected.git_url = "https://github.com/example/skill.git"
            # Historic Git metadata can have one orphaned inline script field;
            # changing unrelated content must not silently rewrite it.
            rejected.script_content = "#!/bin/sh\nexit 0\n"
            await db.commit()
            listing_id, rejected_id, owner_id = listing.id, rejected.id, owner.id

        async with maker() as db:
            owner = await db.get(User, owner_id)
            detail = await component_versions._get_version(
                str(listing_id), "1.3.0", SkillListing, SkillVersion, "skill", db, owner
            )
            assert len(detail["revision"]) == 64
            edited = await skill_files.update_skill_version_draft(
                str(listing_id),
                rejected_id,
                SkillUpdateRequest(
                    observed_revision=detail["revision"],
                    description="Corrected Git release",
                    skill_md_content="---\nname: corrected\ndescription: reviewed\n---\n# Corrected\n",
                ),
                Response(),
                db,
                owner,
            )
            assert edited.version_id == rejected_id
            assert edited.revision != detail["revision"]
            assert edited.files == []
            assert (await db.get(SkillVersion, rejected_id)).script_content == "#!/bin/sh\nexit 0\n"
            submitted = await skill_files.submit_skill_version_draft(
                str(listing_id),
                rejected_id,
                SkillVersionRevisionRequest(observed_revision=edited.revision),
                Response(),
                db,
                owner,
            )
            assert submitted.version_id == rejected_id
            listing = await db.get(SkillListing, listing_id)
            assert listing.latest_version_id == approved_id
            version = await db.get(SkillVersion, rejected_id)
            assert version.status == ListingStatus.pending
            notices = (await db.execute(select(InboxItem).where(InboxItem.subject_id == listing_id))).scalars().all()
            assert len(notices) == 1
            assert notices[0].dedupe_key.endswith(":v1.3.0")
            with pytest.raises(HTTPException) as pending:
                await skill_files.update_skill_version_draft(
                    str(listing_id),
                    rejected_id,
                    SkillUpdateRequest(observed_revision=submitted.revision, description="Unreviewed edit"),
                    Response(),
                    db,
                    owner,
                )
            assert pending.value.status_code == 409
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_git_without_source_cannot_enter_review_queue():
    engine = ds.make_engine()
    maker = await ds.create_schema(engine)
    try:
        async with maker() as db:
            owner = await ds.user(db)
            listing = await ds.skill(db, owner, status=ListingStatus.approved)
            candidate = await ds.add_skill_version(
                db, listing, owner, version="1.3.0", status=ListingStatus.rejected, set_latest=False
            )
            revision = skill_content_revision(listing, candidate)
            await db.commit()
            with pytest.raises(HTTPException) as exc:
                await skill_files.submit_skill_version_draft(
                    str(listing.id),
                    candidate.id,
                    SkillVersionRevisionRequest(observed_revision=revision),
                    Response(),
                    db,
                    owner,
                )
            assert exc.value.status_code == 422
            assert candidate.status == ListingStatus.rejected
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_draft_fork_preserves_approved_pointer_and_supports_isolated_file_edits():
    engine = ds.make_engine()
    maker = await ds.create_schema(engine)
    try:
        async with maker() as db:
            owner = await ds.user(db)
            listing = await ds.skill(db, owner, status=ListingStatus.approved)
            base = (await db.execute(select(SkillVersion).where(SkillVersion.listing_id == listing.id))).scalar_one()
            base.skill_md_content = FIXTURE["snapshot"]["skill_md_content"]
            base.extra_files = [FIXTURE["snapshot"]["extra_files"][0]]
            await db.flush()
            base_revision = skill_content_revision(listing, base)
            listing_id, base_id, owner_id = listing.id, base.id, owner.id
            base_extras = [item.copy() for item in base.extra_files]
            await db.commit()
            request = SkillCandidateDraftRequest(
                base_version_id=base.id,
                observed_base_revision=base_revision,
                version="1.3.0",
                description="Saved draft",
            )
            created = await skill_files.create_skill_candidate_draft(str(listing.id), request, Response(), db, owner)
            assert created.version_id != base.id
            assert len(created.files) == 2
            assert listing.latest_version_id == base.id
            with pytest.raises(HTTPException) as duplicate:
                await skill_files.create_skill_candidate_draft(str(listing.id), request, Response(), db, owner)
            assert duplicate.value.status_code == 409
            await db.rollback()

        async with maker() as db:
            listing = await db.get(SkillListing, listing_id)
            owner = await db.get(type(owner), owner_id)
            candidate = await db.get(SkillVersion, created.version_id)
            assert listing.latest_version_id == base_id
            assert listing.status == ListingStatus.approved
            assert candidate.status == ListingStatus.draft
            assert candidate.base_version_id == base_id
            assert candidate.base_revision == base_revision
            assert candidate.content_revision == created.revision
            assert candidate.extra_files == base_extras
            with pytest.raises(HTTPException) as coordinates:
                await skill_files.update_skill_version_draft(
                    str(listing_id),
                    candidate.id,
                    SkillUpdateRequest(observed_revision=created.revision, git_url="https://example.com/other.git"),
                    Response(),
                    db,
                    owner,
                )
            assert coordinates.value.status_code == 422
            lock = await skill_files.start_skill_version_edit(
                str(listing_id),
                candidate.id,
                SkillVersionRevisionRequest(observed_revision=created.revision),
                db,
                owner,
            )
            assert lock == {"status": "locked"}
            assert candidate.is_editing is True
            edited = await skill_files.patch_skill_files(
                str(listing.id),
                candidate.id,
                SkillFileOperations.model_validate(
                    {
                        "observed_revision": created.revision,
                        "operations": [{"action": "put", "file": {"path": "new.txt", "content": "candidate only"}}],
                    }
                ),
                Response(),
                db,
                owner,
            )
            assert len(edited.files) == 3
            assert listing.latest_version_id == base_id
            unchanged_base = await db.get(SkillVersion, base_id)
            assert len(unchanged_base.extra_files) == 1
            assert len(candidate.extra_files) == 2
            assert candidate.is_editing is False
            with pytest.raises(HTTPException) as stale:
                await skill_files.start_skill_version_edit(
                    str(listing_id),
                    candidate.id,
                    SkillVersionRevisionRequest(observed_revision=created.revision),
                    db,
                    owner,
                )
            assert stale.value.status_code == 409
            with pytest.raises(HTTPException) as gated:
                await skill_files.submit_skill_version_draft(
                    str(listing.id),
                    candidate.id,
                    SkillVersionRevisionRequest(observed_revision=edited.revision),
                    Response(),
                    db,
                    owner,
                )
            assert gated.value.status_code == 409
            assert candidate.status == ListingStatus.draft
            assert listing.latest_version_id == base_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_candidate_rejects_stale_base_or_release_not_above_current():
    engine = ds.make_engine()
    maker = await ds.create_schema(engine)
    try:
        async with maker() as db:
            owner = await ds.user(db)
            listing = await ds.skill(db, owner, status=ListingStatus.approved)
            base = (await db.execute(select(SkillVersion).where(SkillVersion.listing_id == listing.id))).scalar_one()
            base.skill_md_content = FIXTURE["snapshot"]["skill_md_content"]
            await db.flush()
            revision = skill_content_revision(listing, base)
            listing_id, base_id, owner_id = listing.id, base.id, owner.id
            await db.commit()
            for version, observed in (("1.1.0", revision), ("1.3.0", "a" * 64)):
                with pytest.raises(HTTPException) as exc:
                    await skill_files.create_skill_candidate_draft(
                        str(listing_id),
                        SkillCandidateDraftRequest(
                            base_version_id=base_id,
                            observed_base_revision=observed,
                            version=version,
                            description="Candidate",
                        ),
                        Response(),
                        db,
                        owner,
                    )
                assert exc.value.status_code == 409
                await db.rollback()
                owner = await db.get(type(owner), owner_id)
            assert [
                row.id
                for row in (await db.execute(select(SkillVersion).where(SkillVersion.listing_id == listing_id)))
                .scalars()
                .all()
            ] == [base_id]
    finally:
        await engine.dispose()
