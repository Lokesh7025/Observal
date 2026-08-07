# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The events that produce inbox items.

Review is not one choke point. Decisions land in seven endpoints across
``api/routes/review.py``, and a review can be requested from every submit and
publish path plus the visibility flip in
``teamspace.review_publication_to_public``. Every one of them calls a helper
here rather than assembling a delivery of its own, so the recipient rule, the
title and the dedupe key are decided once.

Auto-approved publishes deliberately emit nothing: a team owner publishing
team-visible work approves it for themselves, and there is no pending state for
anyone to act on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger as optic

from models.inbox import InboxKind
from services.inbox import recipients
from services.inbox.delivery import deliver
from services.inbox.registry import Subject

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


def subject_from_entity(entity, subject_type: str, *, version: str | None = None) -> Subject:
    """Build a Subject from a listing or agent row.

    Agents and all five component listings expose the same shape here
    (``namespace``, ``slug``, ``is_private``, ``team_id``), so one reader serves
    every type.
    """
    return Subject(
        type=subject_type,
        id=getattr(entity, "id", None),
        name=getattr(entity, "name", "") or "",
        namespace=getattr(entity, "namespace", None),
        slug=getattr(entity, "slug", None),
        version=version,
        team_id=getattr(entity, "team_id", None),
        is_private=bool(getattr(entity, "is_private", False)),
    )


async def on_review_requested(
    db: AsyncSession,
    entity,
    *,
    subject_type: str,
    actor_id: uuid.UUID | None,
    version: str | None = None,
) -> int:
    """Something entered the review queue: tell everyone who can clear it.

    Call this only when a version is actually left pending. A publish that
    auto-approves has nothing waiting on a reviewer.
    """
    subject = subject_from_entity(entity, subject_type, version=version)
    users = await recipients.reviewers_for(db, entity)
    items = await deliver(
        db,
        kind=InboxKind.review_requested,
        recipients=users,
        subject=subject,
        actor_id=actor_id,
    )
    optic.debug("inbox: review_requested for {} {} -> {} item(s)", subject_type, subject.id, len(items))
    return len(items)


async def on_publish(
    db: AsyncSession,
    entity,
    *,
    subject_type: str,
    actor_id: uuid.UUID | None,
    auto_approved: bool,
    version: str | None = None,
) -> int:
    """A submit or publish finished: queue a review notice only if one is owed.

    Every publish path ends in the same fork — auto-approve, or leave pending —
    so the fork is encoded here once. A call site passing its own
    ``auto_approve`` result cannot forget the rule that self-approved work
    notifies nobody, because there is no pending state for a reviewer to act on.
    """
    if auto_approved:
        return 0
    return await on_review_requested(db, entity, subject_type=subject_type, actor_id=actor_id, version=version)


async def on_review_decided(
    db: AsyncSession,
    entity,
    *,
    subject_type: str,
    approved: bool,
    actor_id: uuid.UUID | None,
    version: str | None = None,
    reason: str | None = None,
    submitter_id: uuid.UUID | None = None,
) -> int:
    """A reviewer acted: tell whoever submitted it.

    ``submitter_id`` overrides the entity lookup for cases where the version
    that was decided has a different author than the listing row — an agent
    version carries ``released_by`` while the agent carries ``created_by``.
    """
    subject = subject_from_entity(entity, subject_type, version=version)
    users = [submitter_id] if submitter_id is not None else recipients.submitter_of(entity)
    items = await deliver(
        db,
        kind=InboxKind.review_approved if approved else InboxKind.review_rejected,
        recipients=[u for u in users if u is not None],
        subject=subject,
        actor_id=actor_id,
        body=reason,
        context={"reason": reason} if reason else None,
    )
    optic.debug(
        "inbox: review_{} for {} {} -> {} item(s)",
        "approved" if approved else "rejected",
        subject_type,
        subject.id,
        len(items),
    )
    return len(items)


async def on_insight_ready(
    db: AsyncSession,
    report,
    *,
    agent_name: str = "",
    requester_id: uuid.UUID | None = None,
) -> int:
    """A report finished generating: tell whoever asked for it.

    The requester only. Notifying the agent owner as well was considered and
    left out: ``batch_generate_insights`` runs weekly for every agent, so owner
    notification would deliver a batch nobody requested. Add it behind an
    explicit preference if it is ever wanted.
    """
    target = requester_id if requester_id is not None else getattr(report, "triggered_by", None)
    if target is None:
        return 0
    subject = Subject(
        type="insight_report",
        id=getattr(report, "id", None),
        name=agent_name or "insight report",
    )
    items = await deliver(
        db,
        kind=InboxKind.insight_ready,
        recipients=[target],
        subject=subject,
        actor_id=None,
        skip_actor=False,  # the requester asked for this; it is the whole point
    )
    return len(items)


async def on_system_notice(
    db: AsyncSession,
    *,
    title: str,
    notice_id: str,
    body: str | None = None,
    context: dict[str, Any] | None = None,
) -> int:
    """An admin/system event worth an operator's attention."""
    users = await recipients.admins(db)
    ctx: dict[str, Any] = {"title": title, "notice_id": notice_id}
    if context:
        ctx.update(context)
    items = await deliver(
        db,
        kind=InboxKind.system_notice,
        recipients=users,
        subject=Subject(type="system", name=title),
        actor_id=None,
        body=body,
        context=ctx,
    )
    return len(items)
