# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Shared registry component matching.

One ranking implementation serves both consumers:

* **Agent insights** (:mod:`services.insights`) — given the signals of an
  insight report, shortlist components the agent could reuse instead of
  building something new.
* **Per-user recommendations** (:mod:`services.user_profile`) — given a
  user's work profile, shortlist components they are likely to want.

Everything here is deterministic (lexical match + popularity prior). No LLM
call happens in this module; callers may re-rank with one afterwards.

Visibility is enforced at the query level via :func:`visibility_clause`,
which is the worker-safe sibling of ``api.deps.apply_visibility_filter``:
it takes an ``org_id``/``user_id`` pair instead of a request-scoped ``User``
so background jobs (which have no authenticated user) can use it too.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger as optic
from sqlalchemy import and_, or_, select

from api.search import keyword_search, keyword_tokens
from models.hook import HookListing, HookVersion
from models.mcp import ListingStatus, McpListing, McpVersion
from models.prompt import PromptListing, PromptVersion
from models.sandbox import SandboxListing, SandboxVersion
from models.skill import SkillListing, SkillVersion

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

# Component type -> (listing model, version model). Order is the tie-break
# order used when trimming a blended shortlist down to ``total_limit``.
COMPONENT_MODELS: dict[str, tuple[Any, Any]] = {
    "skill": (SkillListing, SkillVersion),
    "hook": (HookListing, HookVersion),
    "prompt": (PromptListing, PromptVersion),
    "mcp": (McpListing, McpVersion),
    "sandbox": (SandboxListing, SandboxVersion),
}

ALL_COMPONENT_TYPES: tuple[str, ...] = tuple(COMPONENT_MODELS)

# Popularity is a tie-breaker, not a driver: a wildly popular component must
# not outrank a genuinely relevant one. Capped so a single runaway listing
# cannot dominate the ordering.
_POPULARITY_WEIGHT = 0.4
_POPULARITY_CAP = 50

# Fetch a wider slice than we return so the Python-side blend has room to
# reorder before trimming.
_OVERFETCH = 3


@dataclass
class ComponentCandidate:
    """A registry component that plausibly matches the caller's signals."""

    component_type: str
    id: uuid.UUID
    name: str
    namespace: str
    slug: str
    description: str
    latest_version: str
    download_count: int
    score: float
    category: str | None = None
    matched_terms: list[str] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}/{self.slug}"

    def to_catalog_entry(self) -> dict:
        """Compact dict for embedding in an LLM prompt. Keep this small."""
        entry = {
            "type": self.component_type,
            "id": str(self.id),
            "qualified_name": self.qualified_name,
            "name": self.name,
            "description": self.description[:160],
        }
        if self.category:
            entry["category"] = self.category
        if self.matched_terms:
            entry["matched_on"] = self.matched_terms
        return entry

    def to_api_dict(self) -> dict:
        """Full dict for API responses and UI rendering."""
        return {
            "type": self.component_type,
            "id": str(self.id),
            "name": self.name,
            "namespace": self.namespace,
            "slug": self.slug,
            "qualified_name": self.qualified_name,
            "description": self.description,
            "category": self.category,
            "latest_version": self.latest_version,
            "download_count": self.download_count,
            "matched_on": self.matched_terms,
            "score": round(self.score, 3),
        }


def visibility_clause(listing_model, org_id: uuid.UUID | None, user_id: uuid.UUID | None = None):
    """Return a WHERE clause restricting *listing_model* to visible rows.

    Mirrors ``api.deps.apply_visibility_filter`` but takes plain ids so it
    works inside background jobs that have no request user.

    Rules (identical to the request-scoped filter, minus the admin bypass —
    recommendations must never surface something the *agent owner* could not
    see, even when an admin triggered the run):

    * public listings are always visible
    * private listings are visible to the owning org
    * private listings are visible to their submitter

    ``org_id=None`` means "no org" — such a caller sees public listings plus
    their own private ones. It must never be allowed to match rows whose
    ``owner_org_id`` is also NULL, which is why the org branch is skipped
    entirely rather than compared (``NULL = NULL`` is never true in SQL, but
    being explicit keeps the intent obvious).
    """
    if not hasattr(listing_model, "is_private"):
        # Agents have no privacy flag; everything in the instance is visible.
        return None

    public = listing_model.is_private == False  # noqa: E712
    clauses = [public]

    if org_id is not None:
        clauses.append(and_(listing_model.is_private == True, listing_model.owner_org_id == org_id))  # noqa: E712
    if user_id is not None:
        clauses.append(and_(listing_model.is_private == True, listing_model.submitted_by == user_id))  # noqa: E712

    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def _search_fields(component_type: str, listing_model, version_model) -> list:
    """Text columns worth matching for a component type."""
    fields = [listing_model.name, version_model.description]
    if component_type == "mcp":
        fields.append(listing_model.category)
    elif component_type == "skill":
        fields.append(version_model.task_type)
    elif component_type == "hook":
        fields.append(version_model.event)
    elif component_type == "prompt":
        fields.append(version_model.category)
    return fields


def _row_category(component_type: str, listing, version) -> str | None:
    if component_type == "mcp":
        return listing.category
    if component_type == "skill":
        return getattr(version, "task_type", None)
    if component_type == "hook":
        return getattr(version, "event", None)
    if component_type == "prompt":
        return getattr(version, "category", None)
    return None


def _token_display_map(signals: str) -> dict[str, str]:
    """Map each stemmed search token back to the word the caller actually wrote.

    ``keyword_tokens`` stems trailing plurals ("postgres" -> "postgre"), which
    is fine for matching but reads as a typo when shown to a user. Matching
    still happens on the stem; only the label is restored.
    """
    mapping: dict[str, str] = {}
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", signals or ""):
        for token in keyword_tokens(word):
            mapping.setdefault(token, word.lower())
    return mapping


def _matched_terms(tokens: Sequence[str], haystack: str, display: dict[str, str]) -> list[str]:
    lowered = haystack.lower()
    return [display.get(t, t) for t in tokens if t in lowered]


def build_signal_query(*parts: Iterable[str] | str | None) -> str:
    """Flatten assorted signal collections into one search string.

    Accepts strings and iterables of strings, skipping empties. Order is
    preserved and duplicates are dropped so the token ranking stays stable.
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        if part is None:
            continue
        items = [part] if isinstance(part, str) else list(part)
        for item in items:
            # A None *inside* a collection would stringify to the literal
            # "None" and pollute the search with a meaningless token.
            if item is None:
                continue
            text = str(item).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return " ".join(out)


async def shortlist(
    db: AsyncSession,
    *,
    signals: str,
    component_types: Sequence[str] = ALL_COMPONENT_TYPES,
    org_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    exclude_ids: Iterable[uuid.UUID] = (),
    per_type_limit: int = 6,
    total_limit: int = 24,
) -> list[ComponentCandidate]:
    """Return the most relevant visible components for *signals*.

    Only approved latest versions are considered. When *signals* yields no
    usable tokens the result falls back to a popularity ordering, which is
    what cold-start callers want.
    """
    tokens = keyword_tokens(signals)
    display = _token_display_map(signals)
    excluded = {cid for cid in exclude_ids if cid is not None}
    candidates: list[ComponentCandidate] = []

    for component_type in component_types:
        models = COMPONENT_MODELS.get(component_type)
        if not models:
            optic.warning("registry_recommender: unknown component type {}", component_type)
            continue
        listing_model, version_model = models

        stmt = select(listing_model, version_model).join(
            version_model, listing_model.latest_version_id == version_model.id
        )
        stmt = stmt.where(version_model.status == ListingStatus.approved)

        visibility = visibility_clause(listing_model, org_id, user_id)
        if visibility is not None:
            stmt = stmt.where(visibility)
        if excluded:
            stmt = stmt.where(listing_model.id.notin_(excluded))

        search_filter, search_rank = keyword_search(
            signals,
            _search_fields(component_type, listing_model, version_model),
            name_field=listing_model.name,
        )
        if search_filter is not None:
            stmt = stmt.where(search_filter)
            stmt = stmt.order_by(search_rank.desc(), version_model.download_count.desc())
        else:
            stmt = stmt.order_by(version_model.download_count.desc(), listing_model.created_at.desc())

        stmt = stmt.limit(max(per_type_limit * _OVERFETCH, per_type_limit))

        try:
            rows = (await db.execute(stmt)).all()
        except Exception as e:  # pragma: no cover - defensive, keeps callers alive
            optic.warning("registry_recommender: {} query failed: {}", component_type, e)
            continue

        typed: list[ComponentCandidate] = []
        for listing, version in rows:
            haystack = " ".join(
                str(x)
                for x in (
                    listing.name,
                    version.description,
                    _row_category(component_type, listing, version),
                )
                if x
            )
            matched = _matched_terms(tokens, haystack, display)
            # Relevance dominates; popularity only separates near-ties.
            relevance = float(len(matched))
            popularity = min(version.download_count or 0, _POPULARITY_CAP) / _POPULARITY_CAP
            typed.append(
                ComponentCandidate(
                    component_type=component_type,
                    id=listing.id,
                    name=listing.name,
                    namespace=listing.namespace,
                    slug=listing.slug,
                    description=version.description or "",
                    latest_version=version.version,
                    download_count=version.download_count or 0,
                    score=relevance + popularity * _POPULARITY_WEIGHT,
                    category=_row_category(component_type, listing, version),
                    matched_terms=matched,
                )
            )

        typed.sort(key=lambda c: (-c.score, -c.download_count, c.name.lower()))
        candidates.extend(typed[:per_type_limit])

    candidates.sort(key=lambda c: (-c.score, -c.download_count, c.component_type, c.name.lower()))
    return candidates[:total_limit]


@dataclass
class ResolvedComponent:
    """A registry reference that survived validation."""

    component_type: str
    id: uuid.UUID
    name: str
    namespace: str
    slug: str
    latest_version: str

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}/{self.slug}"

    def to_ref(self) -> dict:
        return {
            "type": self.component_type,
            "id": str(self.id),
            "name": self.name,
            "qualified_name": self.qualified_name,
            "latest_version": self.latest_version,
        }


def coerce_uuid(value: object) -> uuid.UUID | None:
    """Parse a UUID from arbitrary (often LLM-produced) input."""
    if isinstance(value, uuid.UUID):
        return value
    if not value:
        return None
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return None


async def resolve_components(
    db: AsyncSession,
    refs: Iterable[tuple[str, uuid.UUID]],
    *,
    org_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> dict[tuple[str, uuid.UUID], ResolvedComponent]:
    """Validate ``(type, id)`` references against the registry.

    A reference resolves only when the listing exists, its latest version is
    approved, and it is visible to the given org/user. Anything else is
    simply absent from the result — callers treat a miss as "drop it".
    """
    by_type: dict[str, list[uuid.UUID]] = {}
    for component_type, component_id in refs:
        if component_type in COMPONENT_MODELS and component_id is not None:
            by_type.setdefault(component_type, []).append(component_id)

    resolved: dict[tuple[str, uuid.UUID], ResolvedComponent] = {}
    for component_type, ids in by_type.items():
        listing_model, version_model = COMPONENT_MODELS[component_type]
        stmt = (
            select(listing_model, version_model)
            .join(version_model, listing_model.latest_version_id == version_model.id)
            .where(
                listing_model.id.in_(ids),
                version_model.status == ListingStatus.approved,
            )
        )
        visibility = visibility_clause(listing_model, org_id, user_id)
        if visibility is not None:
            stmt = stmt.where(visibility)

        try:
            rows = (await db.execute(stmt)).all()
        except Exception as e:  # pragma: no cover - defensive
            optic.warning("registry_recommender: resolve {} failed: {}", component_type, e)
            continue

        for listing, version in rows:
            resolved[(component_type, listing.id)] = ResolvedComponent(
                component_type=component_type,
                id=listing.id,
                name=listing.name,
                namespace=listing.namespace,
                slug=listing.slug,
                latest_version=version.version,
            )

    return resolved


async def resolve_component_any_type(
    db: AsyncSession,
    component_id: uuid.UUID,
    *,
    org_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    component_types: Sequence[str] = ALL_COMPONENT_TYPES,
) -> ResolvedComponent | None:
    """Resolve a component id when its type is unknown or untrusted.

    Used for LLM output, where the declared type may not match the id.
    """
    refs = [(t, component_id) for t in component_types]
    resolved = await resolve_components(db, refs, org_id=org_id, user_id=user_id)
    for component_type in component_types:
        hit = resolved.get((component_type, component_id))
        if hit:
            return hit
    return None
