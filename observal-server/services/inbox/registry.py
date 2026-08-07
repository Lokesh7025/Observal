# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""One declaration per inbox kind.

Adding a source is adding an entry to ``SPECS`` — the title, the dedupe key, the
canonical link and the exact CLI command all live together here rather than
being spelled out at each call site. Endpoints hand ``deliver()`` a subject and a
kind; they never build a title or a URL themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from models.inbox import InboxKind

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class Subject:
    """What an item is about.

    ``namespace`` and ``slug`` are carried even though the canonical registry
    routes do not exist yet (see ``_registry_url``), so links upgrade with a
    change to one function rather than a data backfill.
    """

    type: str
    id: uuid.UUID | None = None
    name: str = ""
    namespace: str | None = None
    slug: str | None = None
    version: str | None = None
    team_id: uuid.UUID | None = None
    is_private: bool = False
    handle: str | None = None  # teamspaces are addressed by handle, not id


# Registry listing types, as the web router spells them.
_COMPONENT_TYPES = frozenset({"mcp", "skill", "hook", "prompt", "sandbox"})


def _registry_url(subject: Subject) -> str | None:
    """The best link that actually resolves today.

    The roadmap's canonical routes are ``/agents/{namespace}/{slug}`` and
    ``/components/{type}/{namespace}/{slug}``, but the web router currently only
    has the id-addressed ``/agents/$agentId`` and ``/components/$componentId``.
    Emitting the canonical shape now would put a 404 behind every inbox item, so
    this builds the id form and moves to the canonical one when those routes
    land. The item already stores the namespace and slug it would need.
    """
    if subject.type == "team":
        return f"/teamspaces/{subject.handle}" if subject.handle else "/teamspaces"
    if subject.id is None:
        return None
    if subject.type == "agent":
        return f"/agents/{subject.id}"
    if subject.type == "insight_report":
        return f"/insights/{subject.id}"
    if subject.type in _COMPONENT_TYPES:
        return f"/components/{subject.id}"
    return None


def _review_url(subject: Subject) -> str:
    """Reviewers act in the queue, not on the public listing page."""
    return "/review"


def _no_command(subject: Subject, ctx: dict[str, Any]) -> str | None:
    return None


def _review_show_command(subject: Subject, ctx: dict[str, Any]) -> str | None:
    if subject.id is None:
        return None
    suffix = " --agent" if subject.type == "agent" else ""
    return f"observal admin review show {subject.id}{suffix}"


def _pull_command(subject: Subject, ctx: dict[str, Any]) -> str | None:
    """The upgrade command for an installed item.

    Inbox never runs this. It shows the exact command and the user decides.
    """
    name = subject.namespace and subject.slug and f"{subject.namespace}/{subject.slug}"
    target = name or (str(subject.id) if subject.id else subject.name)
    if not target:
        return None
    harness = ctx.get("harness")
    harness_flag = f" --harness {harness}" if harness else ""
    return f"observal pull {target}{harness_flag}"


@dataclass(frozen=True, slots=True)
class KindSpec:
    """How one kind renders and deduplicates."""

    kind: InboxKind
    action_required: bool
    title: Callable[[Subject, dict[str, Any]], str]
    dedupe: Callable[[Subject, dict[str, Any]], str]
    url: Callable[[Subject], str | None] = _registry_url
    command: Callable[[Subject, dict[str, Any]], str | None] = _no_command
    # Items whose subject is a listing are re-checked against team membership at
    # read time. A decision addressed to its own submitter is not: it tells them
    # what happened to something they wrote, which is theirs to know regardless
    # of where the listing ended up.
    recheck_visibility: bool = True
    subject_label: Callable[[Subject], str] = field(default=lambda s: s.name or s.type)


def _label(subject: Subject) -> str:
    return subject.name or (subject.slug or str(subject.id or "item"))


def _versioned(subject: Subject) -> str:
    return f"{_label(subject)} v{subject.version}" if subject.version else _label(subject)


SPECS: dict[InboxKind, KindSpec] = {
    InboxKind.review_requested: KindSpec(
        kind=InboxKind.review_requested,
        action_required=True,
        title=lambda s, c: f"Review requested: {_versioned(s)}",
        dedupe=lambda s, c: f"review_requested:{s.type}:{s.id}:v{s.version or '-'}",
        url=_review_url,
        command=_review_show_command,
    ),
    InboxKind.review_approved: KindSpec(
        kind=InboxKind.review_approved,
        action_required=False,
        title=lambda s, c: f"Approved: {_versioned(s)}",
        dedupe=lambda s, c: f"review_approved:{s.type}:{s.id}:v{s.version or '-'}",
        recheck_visibility=False,
    ),
    InboxKind.review_rejected: KindSpec(
        kind=InboxKind.review_rejected,
        action_required=True,
        title=lambda s, c: f"Changes needed: {_versioned(s)}",
        dedupe=lambda s, c: f"review_rejected:{s.type}:{s.id}:v{s.version or '-'}",
        recheck_visibility=False,
    ),
    InboxKind.review_comment: KindSpec(
        kind=InboxKind.review_comment,
        action_required=False,
        title=lambda s, c: f"New comment on {_label(s)}",
        dedupe=lambda s, c: f"review_comment:{s.type}:{s.id}:{c.get('comment_id', '-')}",
    ),
    InboxKind.change_requested: KindSpec(
        kind=InboxKind.change_requested,
        action_required=True,
        title=lambda s, c: f"Changes requested on {_label(s)}",
        dedupe=lambda s, c: f"change_requested:{s.type}:{s.id}:{c.get('request_id', '-')}",
        recheck_visibility=False,
    ),
    InboxKind.team_join_requested: KindSpec(
        kind=InboxKind.team_join_requested,
        action_required=True,
        title=lambda s, c: f"Join request for {_label(s)}",
        dedupe=lambda s, c: f"team_join_requested:{s.id}:{c.get('requester_id', '-')}",
    ),
    InboxKind.team_join_decided: KindSpec(
        kind=InboxKind.team_join_decided,
        action_required=False,
        title=lambda s, c: f"Join request {c.get('decision', 'decided')}: {_label(s)}",
        dedupe=lambda s, c: f"team_join_decided:{s.id}:{c.get('request_id', '-')}",
        recheck_visibility=False,
    ),
    InboxKind.team_created_pending: KindSpec(
        kind=InboxKind.team_created_pending,
        action_required=True,
        title=lambda s, c: f"Teamspace awaiting approval: {_label(s)}",
        dedupe=lambda s, c: f"team_created_pending:{s.id}",
    ),
    InboxKind.ownership_transfer: KindSpec(
        kind=InboxKind.ownership_transfer,
        action_required=True,
        title=lambda s, c: f"Ownership transfer offered: {_label(s)}",
        dedupe=lambda s, c: f"ownership_transfer:{s.type}:{s.id}:{c.get('transfer_id', '-')}",
    ),
    InboxKind.update_available: KindSpec(
        kind=InboxKind.update_available,
        action_required=False,
        title=lambda s, c: f"Update available: {_label(s)} {c.get('current_version', '?')} → {s.version}",
        # Keyed on the new version, so a newer release is a new fact and a new
        # item rather than a duplicate of the old one.
        dedupe=lambda s, c: f"update_available:{s.type}:{s.id}:{s.version}",
        command=_pull_command,
        # Reported by the CLI about what the reporting user has installed, so
        # there is no team subject to re-check.
        recheck_visibility=False,
    ),
    InboxKind.insight_ready: KindSpec(
        kind=InboxKind.insight_ready,
        action_required=False,
        title=lambda s, c: f"Insight report ready: {_label(s)}",
        dedupe=lambda s, c: f"insight_ready:{s.id}",
        recheck_visibility=False,
    ),
    InboxKind.system_notice: KindSpec(
        kind=InboxKind.system_notice,
        action_required=False,
        title=lambda s, c: str(c.get("title") or "System notice"),
        dedupe=lambda s, c: f"system_notice:{c.get('notice_id', '-')}",
        url=lambda s: None,
        recheck_visibility=False,
    ),
}


def spec_for(kind: InboxKind) -> KindSpec:
    spec = SPECS.get(kind)
    if spec is None:
        raise KeyError(f"No inbox spec registered for kind {kind!r}")
    return spec
