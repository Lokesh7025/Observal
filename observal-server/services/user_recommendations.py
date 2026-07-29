# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Personalised registry recommendations.

Two stages:

1. **Candidates** — deterministic. The user's work profile becomes a search
   string; the shared recommender ranks visible components against it, with
   popularity as a tie-break. Components the user already installed (via an
   agent) or explicitly dismissed are removed.
2. **Explanations** — the "why you" line. Derived from which profile terms
   matched, so it stays truthful and costs nothing.

Cold start (no sessions yet) falls back to popular components rather than
showing an empty rail or inventing relevance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger as optic
from sqlalchemy import select

from models.agent import Agent, AgentVersion
from models.agent_component import AgentComponent
from models.download import AgentDownloadRecord
from models.user_profile import RecommendationFeedback
from services.registry_recommender import (
    ALL_COMPONENT_TYPES,
    ComponentCandidate,
    shortlist,
)

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from services.user_profile import WorkProfile

DEFAULT_LIMIT = 8
CANDIDATE_POOL = 40


@dataclass
class Recommendation:
    """A component suggested to a user, with the reason it was chosen."""

    candidate: ComponentCandidate
    reason: str

    def to_dict(self) -> dict:
        data = self.candidate.to_api_dict()
        data["reason"] = self.reason
        return data


async def _installed_component_ids(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Components the user already has, via agents they installed.

    ``component_download_records`` is never written by the application, so
    adoption is derived from agent installs instead — which is also the
    signal the component leaderboard uses.
    """
    stmt = (
        select(AgentComponent.component_id)
        .join(AgentVersion, AgentComponent.agent_version_id == AgentVersion.id)
        .join(Agent, Agent.latest_version_id == AgentVersion.id)
        .join(AgentDownloadRecord, AgentDownloadRecord.agent_id == Agent.id)
        .where(AgentDownloadRecord.user_id == user_id)
    )
    try:
        return {row[0] for row in (await db.execute(stmt)).all()}
    except Exception as e:
        optic.warning("recommendations: installed lookup failed: {}", e)
        return set()


async def _dismissed_component_ids(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    stmt = select(RecommendationFeedback.component_id).where(
        RecommendationFeedback.user_id == user_id,
        RecommendationFeedback.action.in_(["dismissed", "not_relevant"]),
    )
    try:
        return {row[0] for row in (await db.execute(stmt)).all()}
    except Exception as e:
        optic.warning("recommendations: dismissal lookup failed: {}", e)
        return set()


def _explain(candidate: ComponentCandidate, profile: WorkProfile) -> str:
    """Say why this was recommended, using only what actually matched."""
    if candidate.matched_terms:
        shown = candidate.matched_terms[:3]
        joined = ", ".join(shown)
        return f"Matches your work on {joined}"
    if profile.is_empty():
        return "Popular in your registry"
    return "Popular among components like the ones you use"


async def recommend_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID | None,
    profile: WorkProfile,
    component_types: Sequence[str] = ALL_COMPONENT_TYPES,
    limit: int = DEFAULT_LIMIT,
) -> list[Recommendation]:
    """Rank components for one user. Never raises; returns [] on failure."""
    exclude = await _installed_component_ids(db, user_id)
    exclude |= await _dismissed_component_ids(db, user_id)

    signals = profile.search_signals()

    try:
        candidates = await shortlist(
            db,
            signals=signals,
            component_types=component_types,
            org_id=org_id,
            user_id=user_id,
            exclude_ids=exclude,
            per_type_limit=max(limit, 4),
            total_limit=CANDIDATE_POOL,
        )
    except Exception as e:
        optic.warning("recommendations: shortlist failed for {}: {}", user_id, e)
        return []

    recommendations = [Recommendation(candidate=c, reason=_explain(c, profile)) for c in candidates[:limit]]

    # A thin profile can match almost nothing, leaving a near-empty rail even
    # though the registry is full. Top up with popular components so the page
    # still helps — labelled as popularity, not as a personal match.
    if len(recommendations) < limit:
        already = exclude | {r.candidate.id for r in recommendations}
        try:
            filler = await shortlist(
                db,
                signals="",
                component_types=component_types,
                org_id=org_id,
                user_id=user_id,
                exclude_ids=already,
                per_type_limit=limit,
                total_limit=limit - len(recommendations),
            )
        except Exception as e:
            optic.warning("recommendations: popularity top-up failed for {}: {}", user_id, e)
            filler = []
        recommendations.extend(Recommendation(candidate=c, reason="Popular in your registry") for c in filler)

    optic.debug(
        "recommendations: user={} profile_sessions={} returned={}",
        user_id,
        profile.session_count,
        len(recommendations),
    )
    return recommendations


async def record_feedback(
    db: AsyncSession,
    user_id: uuid.UUID,
    component_type: str,
    component_id: uuid.UUID,
    action: str,
) -> None:
    """Persist a dismissal (or install) so it is honoured next time."""
    existing = (
        await db.execute(
            select(RecommendationFeedback).where(
                RecommendationFeedback.user_id == user_id,
                RecommendationFeedback.component_type == component_type,
                RecommendationFeedback.component_id == component_id,
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.action = action
    else:
        db.add(
            RecommendationFeedback(
                user_id=user_id,
                component_type=component_type,
                component_id=component_id,
                action=action,
            )
        )
    await db.commit()
