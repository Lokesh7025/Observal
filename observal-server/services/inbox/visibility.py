# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Re-checking who may still read an item.

Authorization is resolved when an item is written, which is correct at that
instant. Membership can be revoked afterwards, and a user removed from a
teamspace must not keep reading a private component's name out of their inbox.
So the list and detail endpoints check again at read time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import or_, select, true

from api.deps import can_see_private_listings
from models.inbox import InboxItem, InboxKind
from models.team import TeamMembership
from services.inbox.registry import SPECS, spec_for

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from models.user import User

# Kinds that opt out of the membership re-check, resolved once from the registry
# so the SQL predicate and the row-level check cannot name different sets.
_NO_RECHECK_KINDS: tuple[InboxKind, ...] = tuple(kind for kind, spec in SPECS.items() if not spec.recheck_visibility)


def visible_filter(user: User):
    """SQL predicate matching :func:`visible_to`, for counts and pagination.

    This is the set-level twin of ``visible_to`` and must stay identical to it,
    the same way ``apply_visibility_filter`` and
    ``check_listing_visibility_async`` mirror each other in ``api.deps``.

    It exists because post-filtering rows is not enough on its own: a ``total``
    or a badge count computed over unfiltered rows still discloses that hidden
    items exist. Someone removed from a teamspace could watch the count move and
    infer activity on a private component they can no longer read. Filtering in
    SQL keeps the aggregates, the page size, and the rows telling one story.
    """
    if can_see_private_listings(user):
        return true()
    member = (
        select(TeamMembership.id)
        .where(
            TeamMembership.team_id == InboxItem.team_id,
            TeamMembership.user_id == user.id,
        )
        .correlate(InboxItem)
        .exists()
    )
    return or_(
        InboxItem.is_private_subject == False,  # noqa: E712
        InboxItem.kind.in_(_NO_RECHECK_KINDS),
        InboxItem.team_id.is_(None),
        member,
    )


async def visible_to(db: AsyncSession, item: InboxItem, user: User) -> bool:
    """Whether this recipient may still see this item.

    The question here is "may this user still SEE the subject", which is
    membership — the same rule as ``api.deps.check_listing_visibility_async``.
    It is deliberately NOT ``teamspace.can_review``/``ReviewScope``: that
    answers "may they ACT on it", and a plain team member may see a private
    listing they have no authority to review. Using the review predicate here
    would hide a member's own decision notices from them.

    Items that fail this are omitted by the caller, never redacted. A redacted
    placeholder still discloses that something exists.
    """
    if not item.is_private_subject:
        return True

    # A decision addressed to its own submitter reports what happened to work
    # they wrote. That stays theirs to read even if the listing has since moved
    # somewhere they cannot follow.
    if not spec_for(item.kind).recheck_visibility:
        return True

    if can_see_private_listings(user):
        return True

    if item.team_id is None:
        # Private with no teamspace is a personal subject: only the recipient,
        # who is already the only person holding this row.
        return True

    membership = await db.scalar(
        select(TeamMembership.id).where(
            TeamMembership.team_id == item.team_id,
            TeamMembership.user_id == user.id,
        )
    )
    return membership is not None


async def filter_visible(db: AsyncSession, items: list[InboxItem], user: User) -> list[InboxItem]:
    """Drop items the recipient may no longer see."""
    kept: list[InboxItem] = []
    for item in items:
        if await visible_to(db, item, user):
            kept.append(item)
    return kept
