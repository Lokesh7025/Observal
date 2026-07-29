# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Personalised registry recommendations.

A user's work profile is private to them: every endpoint here operates on
``current_user`` only. There is deliberately no route to read someone else's
profile or recommendations.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger as optic
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db, get_project_id, require_role
from models.user import User, UserRole
from services.registry_recommender import ALL_COMPONENT_TYPES
from services.user_profile import WorkProfile, get_or_build_profile
from services.user_recommendations import recommend_for_user, record_feedback

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])

_VALID_ACTIONS = {"dismissed", "not_relevant", "installed"}

# The UI addresses component types in plural form ("mcps", "sandboxes").
# `rstrip("s")` cannot be used here: it would turn "sandbox" into "sandbo".
_TYPE_ALIASES: dict[str, str] = {
    **{t: t for t in ALL_COMPONENT_TYPES},
    "skills": "skill",
    "hooks": "hook",
    "prompts": "prompt",
    "mcps": "mcp",
    "sandboxes": "sandbox",
}


def _normalize_type(raw: str) -> str:
    """Map a plural or singular type name onto its canonical singular form."""
    normalized = _TYPE_ALIASES.get(raw.strip().lower())
    if normalized is None:
        raise HTTPException(status_code=400, detail=f"Unknown component type {raw!r}")
    return normalized


class RecommendationItem(BaseModel):
    type: str
    id: str
    name: str
    namespace: str
    slug: str
    qualified_name: str
    description: str
    category: str | None = None
    latest_version: str
    download_count: int
    matched_on: list[str] = Field(default_factory=list)
    score: float
    reason: str


class RecommendationsResponse(BaseModel):
    items: list[RecommendationItem]
    # False when the user has no session history yet, so the UI can label the
    # rail honestly instead of implying personalisation that did not happen.
    personalized: bool
    profile_sessions: int
    topics: list[str] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    component_type: str
    component_id: uuid.UUID
    action: str


@router.get("/me", response_model=RecommendationsResponse)
async def my_recommendations(
    limit: int = Query(8, ge=1, le=24),
    type_: str | None = Query(None, alias="type"),
    refresh: bool = Query(False, description="Recompute the profile instead of using the cache"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Components recommended for the signed-in user."""
    component_types = (_normalize_type(type_),) if type_ else ALL_COMPONENT_TYPES

    try:
        profile = await get_or_build_profile(
            db,
            current_user.id,
            get_project_id(current_user),
            force=refresh,
        )
    except Exception as e:
        # Telemetry problems must not take down the registry home page.
        optic.warning("recommendations: profile unavailable for {}: {}", current_user.id, e)
        profile = WorkProfile()

    recommendations = await recommend_for_user(
        db,
        user_id=current_user.id,
        org_id=current_user.org_id,
        profile=profile,
        component_types=component_types,
        limit=limit,
    )

    return RecommendationsResponse(
        items=[RecommendationItem(**r.to_dict()) for r in recommendations],
        personalized=not profile.is_empty(),
        profile_sessions=profile.session_count,
        topics=profile.topics,
    )


@router.post("/feedback", status_code=204)
async def submit_feedback(
    req: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(UserRole.user)),
):
    """Record a dismissal so the component stops being recommended."""
    component_type = _normalize_type(req.component_type)
    if req.action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action {req.action!r}")

    await record_feedback(db, current_user.id, component_type, req.component_id, req.action)
