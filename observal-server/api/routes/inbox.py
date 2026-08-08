# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""The per-user inbox API.

Every route is scoped to ``current_user`` and there is no admin view of another
user's inbox. That is a different feature and a privacy decision nobody has
made; adding it here by accident would be the wrong way to make it.
"""

# No `from __future__ import annotations` here, matching the other route
# modules: FastAPI resolves path and query parameter annotations at runtime.
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger as optic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db, require_role
from models.inbox import InboxItem, InboxItemEvent, InboxKind, InboxState
from models.user import User, UserRole
from schemas.inbox import (
    BulkReadResponse,
    InboxCountResponse,
    InboxItemDetailResponse,
    InboxItemEventResponse,
    InboxItemResponse,
    InboxListResponse,
    OutdatedReportRequest,
    OutdatedReportResponse,
)
from services.inbox import delivery, visibility
from services.inbox.registry import Subject

router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])

# The list endpoint re-checks visibility per row after the query, so it reads a
# window wider than the requested page to refill what the check drops. Anything
# beyond this and the caller should be paginating rather than scrolling.
_MAX_PAGE_SIZE = 100


def _to_response(item: InboxItem) -> InboxItemResponse:
    return InboxItemResponse(
        id=item.id,
        kind=item.kind.value,
        state=item.state.value,
        read=item.read_at is not None,
        read_at=item.read_at,
        action_required=item.action_required,
        title=item.title,
        body=item.body,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        subject_namespace=item.subject_namespace,
        subject_slug=item.subject_slug,
        action_url=item.action_url,
        action_command=item.action_command,
        actor_id=item.actor_id,
        team_id=item.team_id,
        payload=item.payload or {},
        created_at=item.created_at,
        resolved_at=item.resolved_at,
    )


def _apply_filters(
    stmt,
    *,
    state: InboxState | None,
    kind: InboxKind | None,
    action_required: bool | None,
    unread: bool | None,
    subject_type: str | None,
):
    if state is not None:
        stmt = stmt.where(InboxItem.state == state)
    if kind is not None:
        stmt = stmt.where(InboxItem.kind == kind)
    if action_required is not None:
        stmt = stmt.where(InboxItem.action_required == action_required)
    if unread is not None:
        stmt = stmt.where(InboxItem.read_at.is_(None) if unread else InboxItem.read_at.is_not(None))
    if subject_type is not None:
        stmt = stmt.where(InboxItem.subject_type == subject_type)
    return stmt


async def _load_own_item(db: AsyncSession, item_id: uuid.UUID, user: User) -> InboxItem:
    """Fetch one item belonging to this caller, or 404.

    Another user's item answers 404 rather than 403: the caller has no way to
    learn an id they do not own, and confirming that one exists would be a
    disclosure in itself.
    """
    item = (
        await db.execute(select(InboxItem).where(InboxItem.id == item_id, InboxItem.user_id == user.id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if not await visibility.visible_to(db, item, user):
        # Same answer as a missing row. The subject has become invisible to this
        # user, so the item must not confirm it exists either.
        raise HTTPException(status_code=404, detail="Inbox item not found")
    return item


@router.get("", response_model=InboxListResponse)
async def list_inbox(
    state: InboxState | None = Query(None),
    kind: InboxKind | None = Query(None),
    action_required: bool | None = Query(None),
    unread: bool | None = Query(None),
    subject_type: str | None = Query(None, max_length=32),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=_MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """List this caller's items, newest first.

    Visibility is applied in SQL, so ``total``, the page size, and the rows all
    describe the same set. Counting first and filtering afterwards would leak:
    the total would still include items whose subject the caller can no longer
    see, and a page could come back short with no explanation.
    """
    optic.trace("inbox list for user {}", current_user.id)
    base = select(InboxItem).where(
        InboxItem.user_id == current_user.id,
        visibility.visible_filter(current_user),
    )
    base = _apply_filters(
        base,
        state=state,
        kind=kind,
        action_required=action_required,
        unread=unread,
        subject_type=subject_type,
    )

    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0

    offset = (page - 1) * page_size
    rows = (
        (
            await db.execute(
                base.order_by(InboxItem.created_at.desc(), InboxItem.id.desc()).offset(offset).limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return InboxListResponse(
        items=[_to_response(item) for item in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/count", response_model=InboxCountResponse)
async def inbox_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Badge counts.

    Unread counts every unread item regardless of state, because an item that
    was resolved without ever being opened is still something the user has not
    seen. Action-required counts only OPEN items: work already done is not
    outstanding.

    All three counts carry the visibility filter. A badge that ticks up for an
    item the caller cannot open would disclose the item just as surely as
    listing it.
    """
    mine = (InboxItem.user_id == current_user.id, visibility.visible_filter(current_user))
    unread = (
        await db.execute(select(func.count(InboxItem.id)).where(*mine, InboxItem.read_at.is_(None)))
    ).scalar() or 0
    action = (
        await db.execute(
            select(func.count(InboxItem.id)).where(
                *mine,
                InboxItem.action_required == True,  # noqa: E712
                InboxItem.state == InboxState.open,
            )
        )
    ).scalar() or 0
    open_total = (
        await db.execute(select(func.count(InboxItem.id)).where(*mine, InboxItem.state == InboxState.open))
    ).scalar() or 0
    return InboxCountResponse(unread=unread, action_required=action, open=open_total)


@router.get("/{item_id}", response_model=InboxItemDetailResponse)
async def get_inbox_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    item = await _load_own_item(db, item_id, current_user)
    history = (
        (
            await db.execute(
                select(InboxItemEvent)
                .where(InboxItemEvent.item_id == item.id)
                .order_by(InboxItemEvent.created_at.asc(), InboxItemEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    base = _to_response(item)
    return InboxItemDetailResponse(
        **base.model_dump(),
        history=[
            InboxItemEventResponse(
                id=row.id,
                event=row.event,
                actor_id=row.actor_id,
                detail=row.detail,
                created_at=row.created_at,
            )
            for row in history
        ],
    )


@router.post("/{item_id}/read", response_model=InboxItemResponse)
async def mark_item_read(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Mark read. This does NOT change ``state`` and does not resolve anything."""
    item = await _load_own_item(db, item_id, current_user)
    delivery.mark_read(db, item, actor_id=current_user.id)
    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.post("/{item_id}/unread", response_model=InboxItemResponse)
async def mark_item_unread(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    item = await _load_own_item(db, item_id, current_user)
    delivery.mark_unread(db, item, actor_id=current_user.id)
    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.post("/{item_id}/done", response_model=InboxItemResponse)
async def mark_item_done(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    item = await _load_own_item(db, item_id, current_user)
    delivery.resolve(db, item, state=InboxState.done, actor_id=current_user.id)
    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.post("/{item_id}/dismiss", response_model=InboxItemResponse)
async def dismiss_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    item = await _load_own_item(db, item_id, current_user)
    delivery.resolve(db, item, state=InboxState.dismissed, actor_id=current_user.id)
    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.post("/{item_id}/reopen", response_model=InboxItemResponse)
async def reopen_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Undo a done or dismiss. Resolving by mistake must be recoverable."""
    item = await _load_own_item(db, item_id, current_user)
    delivery.resolve(db, item, state=InboxState.open, actor_id=current_user.id)
    await db.commit()
    await db.refresh(item)
    return _to_response(item)


@router.post("/read-all", response_model=BulkReadResponse)
async def read_all(
    state: InboxState | None = Query(None),
    kind: InboxKind | None = Query(None),
    action_required: bool | None = Query(None),
    subject_type: str | None = Query(None, max_length=32),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Mark everything matching the active filter as read.

    Filter-scoped, not global. A blanket read-all over an actionable feed is a
    footgun: it clears the unread signal on work the user has never looked at.
    Passing no filters still means "everything currently unread", which is the
    caller's explicit choice rather than a hidden default.
    """
    # Items whose subject the caller can no longer see are excluded in SQL, so
    # they are never silently marked read on the caller's behalf.
    stmt = select(InboxItem).where(
        InboxItem.user_id == current_user.id,
        InboxItem.read_at.is_(None),
        visibility.visible_filter(current_user),
    )
    stmt = _apply_filters(
        stmt,
        state=state,
        kind=kind,
        action_required=action_required,
        unread=None,
        subject_type=subject_type,
    )
    rows = (await db.execute(stmt)).scalars().all()

    updated = 0
    for item in rows:
        if delivery.mark_read(db, item, actor_id=current_user.id):
            updated += 1
    await db.commit()
    return BulkReadResponse(updated=updated)


@router.post("/outdated-report", response_model=OutdatedReportResponse)
async def outdated_report(
    req: OutdatedReportRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Record outdated findings the CLI computed from the local lock file.

    The server cannot compute this itself: ``AgentDownloadRecord`` knows who has
    an agent but not which version, and ``ComponentDownloadRecord`` knows the
    version but not the user. The lock file is the only source with both, so the
    exact comparison stays in ``observal outdated`` and the result is reported
    here.

    Reported versions are NOT validated against the registry. Items land only in
    the reporting user's own inbox, so a fabricated report harms nobody else.
    That stops being true the moment these rows feed anything shared — an
    aggregate, an admin view — and the trade must be revisited then.
    """
    created = 0
    superseded = 0

    for entry in req.items:
        subject = Subject(
            type=entry.type,
            id=entry.component_id,
            name=entry.name or (entry.slug or ""),
            namespace=entry.namespace,
            slug=entry.slug,
            version=entry.latest_version,
        )
        context = {
            "current_version": entry.current_version,
            "latest_version": entry.latest_version,
            "harness": entry.harness,
        }
        item = await delivery.deliver_one(
            db,
            kind=InboxKind.update_available,
            user_id=current_user.id,
            subject=subject,
            context=context,
        )
        if item is None:
            continue
        created += 1

        # Older notices for the same component are about a version that is no
        # longer the latest. They are closed with a history entry rather than
        # deleted, so the trail of what was offered survives.
        stale = (
            (
                await db.execute(
                    select(InboxItem).where(
                        InboxItem.user_id == current_user.id,
                        InboxItem.kind == InboxKind.update_available,
                        InboxItem.subject_id == entry.component_id,
                        InboxItem.state == InboxState.open,
                        InboxItem.id != item.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        superseded += await delivery.supersede(db, user_id=current_user.id, keep=item, others=list(stale))

    await db.commit()
    optic.debug("inbox: outdated report from {} -> {} new, {} superseded", current_user.id, created, superseded)
    return OutdatedReportResponse(created=created, superseded=superseded)
