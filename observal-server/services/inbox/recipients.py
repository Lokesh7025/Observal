# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Who receives an item.

Every resolver here is a thin wrapper over authorization that already exists.
No call site writes its own recipient query: one place to read, one place to
audit, and one place that has to change when the rules do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from models.team import TeamMembership, TeamRole
from models.user import User, UserRole
from services.teamspace import REVIEWING_TEAM_ROLES

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

# Global roles that may review the public catalog. Mirrors
# ``teamspace.is_global_reviewer``.
_GLOBAL_REVIEWER_ROLES = (UserRole.reviewer, UserRole.admin, UserRole.super_admin)
_ADMIN_ROLES = (UserRole.admin, UserRole.super_admin)


async def _user_ids(db: AsyncSession, stmt) -> list[uuid.UUID]:
    return list((await db.execute(stmt)).scalars().all())


async def reviewers_for(db: AsyncSession, entity) -> list[uuid.UUID]:
    """Everyone who may review this entity.

    This is the set inverse of ``teamspace.can_review``: that function answers
    "may this caller review this item", and delivery needs "who are those
    callers". The two must agree exactly, which is why the arms below are
    written to mirror it line for line rather than approximating it —
    ``test_reviewers_for_matches_can_review_exactly`` in ``tests/test_inbox.py``
    asserts the agreement over a user matrix and fails the build if either side
    drifts.

    can_review: admins review everything; a private item belongs to its
    teamspace's owners and reviewers; a private item with no teamspace is a
    personal listing and stays admin-only; a public item is the global catalog.
    """
    admins = await _user_ids(db, select(User.id).where(User.role.in_(_ADMIN_ROLES)))

    if getattr(entity, "is_private", False):
        team_id = getattr(entity, "team_id", None)
        if team_id is None:
            # Personal private listing: nobody holds a team role over it.
            return admins
        team_reviewers = await _user_ids(
            db,
            select(TeamMembership.user_id).where(
                TeamMembership.team_id == team_id,
                TeamMembership.role.in_(REVIEWING_TEAM_ROLES),
            ),
        )
        return _dedupe(admins + team_reviewers)

    return await _user_ids(db, select(User.id).where(User.role.in_(_GLOBAL_REVIEWER_ROLES)))


async def team_owners(db: AsyncSession, team_id: uuid.UUID) -> list[uuid.UUID]:
    """Owners of one teamspace — join requests and transfer offers land here."""
    return await _user_ids(
        db,
        select(TeamMembership.user_id).where(
            TeamMembership.team_id == team_id,
            TeamMembership.role == TeamRole.owner,
        ),
    )


async def admins(db: AsyncSession) -> list[uuid.UUID]:
    """Admins and super_admins, for system notices."""
    return await _user_ids(db, select(User.id).where(User.role.in_(_ADMIN_ROLES)))


def submitter_of(entity) -> list[uuid.UUID]:
    """The user a decision is addressed to.

    Listings carry ``submitted_by``; agent versions carry ``released_by``. An
    agent listing itself falls back to ``created_by``.
    """
    for attr in ("submitted_by", "released_by", "created_by"):
        value = getattr(entity, attr, None)
        if value is not None:
            return [value]
    return []


def _dedupe(ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Preserve order, drop repeats.

    An admin who is also a team reviewer appears in both arms above. The unique
    constraint would catch the duplicate anyway, but resolving it here keeps the
    delivery count honest.
    """
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for value in ids:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
