# SPDX-FileCopyrightText: 2026 Kaushik Kumar <kaushikrjpm10@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Serialize edits and review decisions for the same skill version.

Always lock the listing before its version. Re-read the version under the lock:
selectin-loaded relationship objects from an earlier query can be stale after a
concurrent writer commits while this transaction waits for the listing lock.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.skill import SkillListing, SkillVersion


async def lock_skill_version(
    db: AsyncSession, listing_id: uuid.UUID, version_id: uuid.UUID
) -> tuple[uuid.UUID | None, SkillVersion]:
    latest_id = (
        await db.execute(select(SkillListing.latest_version_id).where(SkillListing.id == listing_id).with_for_update())
    ).scalar_one_or_none()
    if latest_id is None:
        raise HTTPException(status_code=409, detail="Skill listing changed during review or editing")
    version = (
        await db.execute(
            select(SkillVersion)
            .where(SkillVersion.id == version_id, SkillVersion.listing_id == listing_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=409, detail="Skill version changed during review or editing")
    return latest_id, version


async def should_promote_skill_version(db: AsyncSession, latest_id: uuid.UUID, candidate: SkillVersion) -> bool:
    """Compare against the listing's fresh, locked latest pointer, not its cached relationship."""
    if latest_id == candidate.id:
        return True
    current_version = (
        await db.execute(select(SkillVersion.version).where(SkillVersion.id == latest_id))
    ).scalar_one_or_none()
    if current_version is None:
        raise HTTPException(status_code=409, detail="Skill latest version changed during review")

    def semver_key(value: str) -> tuple:
        base, separator, prerelease = value.partition("-")
        try:
            numbers = tuple(int(part) for part in base.split("."))
            if len(numbers) != 3:
                raise ValueError("Expected three version components")
            identifiers = (
                tuple((0, int(part)) if part.isdigit() else (1, part) for part in prerelease.split("."))
                if separator
                else ()
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Skill version is not comparable") from exc
        # For the same numeric release, stable is newer than every prerelease.
        return (*numbers, 0 if separator else 1, identifiers)

    return semver_key(candidate.version) >= semver_key(current_version)
