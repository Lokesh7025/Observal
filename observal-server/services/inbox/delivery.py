# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Writing inbox items and moving them through their lifecycle.

Delivery is transactional. ``deliver()`` writes rows in the caller's own
transaction, so inbox items commit exactly when the thing they describe commits
and roll back with it. It is deliberately NOT routed through
``services.events.EventBus``: that bus awaits handlers inline and swallows their
exceptions, logging that "this handler's side-effects are lost". A lost handler
would be a lost inbox item, which is the one thing this feature may not do.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loguru import logger as optic
from sqlalchemy.exc import IntegrityError

from models.inbox import InboxItem, InboxItemEvent, InboxKind, InboxState
from services.inbox.registry import Subject, spec_for

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


def record_event(
    db: AsyncSession,
    item: InboxItem,
    event: str,
    *,
    actor_id: uuid.UUID | None = None,
    detail: str | None = None,
) -> InboxItemEvent:
    """Append one history row. Callers must be inside the item's transaction."""
    row = InboxItemEvent(item_id=item.id, event=event, actor_id=actor_id, detail=detail)
    db.add(row)
    return row


async def deliver_one(
    db: AsyncSession,
    *,
    kind: InboxKind,
    user_id: uuid.UUID,
    subject: Subject,
    actor_id: uuid.UUID | None = None,
    body: str | None = None,
    context: dict[str, Any] | None = None,
    action_required: bool | None = None,
) -> InboxItem | None:
    """Deliver one item to one recipient. Returns None if already delivered.

    Idempotency is enforced by the ``(user_id, dedupe_key)`` unique constraint
    and recovered from inside a SAVEPOINT. The savepoint is load-bearing: this
    runs in the originating request's transaction, so an ``IntegrityError``
    escaping to a plain ``db.rollback()`` would discard the caller's own
    uncommitted work — a redelivered approval notice would undo the approval —
    and every sibling recipient already flushed in this batch. Postgres also
    refuses further statements after an error unless a savepoint absorbs it, so
    there is no "roll back and carry on" without one.

    Do not copy the recovery in ``services/user_recommendations.py`` here. It
    rolls the whole session back, which is safe only because it owns its
    transaction and commits at top level.
    """
    ctx = context or {}
    spec = spec_for(kind)

    item = InboxItem(
        user_id=user_id,
        kind=kind,
        state=InboxState.open,
        action_required=spec.action_required if action_required is None else action_required,
        title=spec.title(subject, ctx)[:255],
        body=body,
        subject_type=subject.type,
        subject_id=subject.id,
        subject_namespace=subject.namespace,
        subject_slug=subject.slug,
        action_url=_truncate(spec.url(subject), 500),
        action_command=_truncate(spec.command(subject, ctx), 500),
        actor_id=actor_id,
        team_id=subject.team_id,
        is_private_subject=bool(subject.is_private),
        dedupe_key=spec.dedupe(subject, ctx)[:255],
        payload=dict(ctx),
    )

    try:
        async with db.begin_nested():  # SAVEPOINT: portable across SQLite and Postgres
            db.add(item)
            await db.flush()
    except IntegrityError:
        # Already delivered. Not an error, and not a reason to disturb either
        # the enclosing transaction or the rest of the batch.
        optic.trace("inbox item already delivered for user {} kind {}", user_id, kind.value)
        return None

    record_event(db, item, "created", actor_id=actor_id)
    return item


async def deliver(
    db: AsyncSession,
    *,
    kind: InboxKind,
    recipients: Iterable[uuid.UUID],
    subject: Subject,
    actor_id: uuid.UUID | None = None,
    body: str | None = None,
    context: dict[str, Any] | None = None,
    action_required: bool | None = None,
    skip_actor: bool = True,
) -> list[InboxItem]:
    """Fan out one fact to its recipients, in the caller's transaction.

    ``skip_actor`` keeps a user from being told about their own action. A team
    reviewer who approves an item is in the reviewer set for it; delivering to
    them would put their own click in their queue.
    """
    delivered: list[InboxItem] = []
    for user_id in _unique(recipients):
        if skip_actor and actor_id is not None and user_id == actor_id:
            continue
        item = await deliver_one(
            db,
            kind=kind,
            user_id=user_id,
            subject=subject,
            actor_id=actor_id,
            body=body,
            context=context,
            action_required=action_required,
        )
        if item is not None:
            delivered.append(item)
    return delivered


async def supersede(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    keep: InboxItem,
    others: Sequence[InboxItem],
) -> int:
    """Resolve older items that a newer fact has replaced.

    A newer version is a new ``dedupe_key`` and therefore a new item; the
    previous one is not a duplicate to be swallowed but a fact that is no longer
    true. It is closed with its own history entry rather than deleted, because
    the point of this feature is that work does not silently disappear.
    """
    now = datetime.now(UTC)
    count = 0
    for stale in others:
        if stale.id == keep.id or stale.state != InboxState.open:
            continue
        stale.state = InboxState.done
        stale.resolved_at = now
        record_event(db, stale, "superseded", detail=f"Superseded by {keep.dedupe_key}")
        count += 1
    return count


def mark_read(db: AsyncSession, item: InboxItem, *, actor_id: uuid.UUID | None = None) -> bool:
    """Mark an item read WITHOUT touching its lifecycle state.

    The two axes are independent by design. An item that is read is still open
    work, still counted by the needs-action filter, and still in the default
    list. Collapsing these into one boolean is the failure this feature exists
    to prevent.
    """
    if item.read_at is not None:
        return False
    item.read_at = datetime.now(UTC)
    record_event(db, item, "read", actor_id=actor_id)
    return True


def mark_unread(db: AsyncSession, item: InboxItem, *, actor_id: uuid.UUID | None = None) -> bool:
    if item.read_at is None:
        return False
    item.read_at = None
    record_event(db, item, "unread", actor_id=actor_id)
    return True


def resolve(
    db: AsyncSession,
    item: InboxItem,
    *,
    state: InboxState,
    actor_id: uuid.UUID | None = None,
) -> bool:
    """Move an item to done or dismissed, or reopen it."""
    if item.state == state:
        return False
    item.state = state
    if state == InboxState.open:
        item.resolved_at = None
        record_event(db, item, "reopened", actor_id=actor_id)
    else:
        item.resolved_at = datetime.now(UTC)
        record_event(db, item, state.value, actor_id=actor_id)
    return True


def _unique(values: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    seen: set[uuid.UUID] = set()
    out: list[uuid.UUID] = []
    for value in values:
        if value is not None and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]
