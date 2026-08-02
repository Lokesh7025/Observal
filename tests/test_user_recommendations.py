# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-user registry recommendations.

Covers the deterministic half: profile shaping, exclusion of things the user
already has or dismissed, private-listing visibility, and honest cold-start
behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from models.agent import Agent, AgentStatus, AgentVersion
from models.agent_component import AgentComponent
from models.base import Base
from models.download import AgentDownloadRecord
from models.mcp import ListingStatus, McpListing, McpVersion
from models.skill import SkillListing, SkillVersion
from models.user import User
from services.user_profile import WorkProfile, _mcp_server_name, _topics_for
from services.user_recommendations import recommend_for_user, record_feedback

if TYPE_CHECKING:
    import uuid


@pytest.fixture()
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session
        await session.rollback()
    await engine.dispose()


async def _user(db: AsyncSession, email: str = "dev@example.com") -> User:
    user = User(email=email, username=email.split("@")[0], name=email)
    db.add(user)
    await db.flush()
    return user


async def _skill(
    db: AsyncSession,
    *,
    name: str,
    submitter: User,
    description: str,
    downloads: int = 0,
    is_private: bool = False,
) -> SkillListing:
    listing = SkillListing(
        name=name,
        namespace="test",
        slug=name,
        owner="t",
        submitted_by=submitter.id,
        is_private=is_private,
    )
    db.add(listing)
    await db.flush()
    version = SkillVersion(
        listing_id=listing.id,
        version="1.0.0",
        description=description,
        status=ListingStatus.approved,
        task_type="general",
        delivery_mode="registry_direct",
        download_count=downloads,
        released_by=submitter.id,
        released_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()
    listing.latest_version_id = version.id
    await db.flush()
    return listing


async def _mcp(db: AsyncSession, *, name: str, submitter: User, description: str) -> McpListing:
    listing = McpListing(
        name=name, namespace="test", slug=name, owner="t", category="databases", submitted_by=submitter.id
    )
    db.add(listing)
    await db.flush()
    version = McpVersion(
        listing_id=listing.id,
        version="1.0.0",
        description=description,
        status=ListingStatus.approved,
        released_by=submitter.id,
        released_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()
    listing.latest_version_id = version.id
    await db.flush()
    return listing


async def _installed_agent_with(db: AsyncSession, user: User, component_id: uuid.UUID) -> None:
    """Give `user` an installed agent that bundles `component_id`."""
    agent = Agent(name="a", namespace="test", slug="a", owner="t", created_by=user.id)
    db.add(agent)
    await db.flush()
    version = AgentVersion(
        agent_id=agent.id,
        version="1.0.0",
        description="d",
        prompt="p",
        model_name="claude-sonnet-4",
        status=AgentStatus.approved,
        released_by=user.id,
        released_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()
    agent.latest_version_id = version.id
    db.add(
        AgentComponent(
            agent_version_id=version.id,
            component_type="skill",
            component_id=component_id,
            component_name="x",
            resolved_version="1.0.0",
            order_index=0,
        )
    )
    db.add(AgentDownloadRecord(agent_id=agent.id, user_id=user.id, source="cli"))
    await db.flush()


DB_PROFILE = WorkProfile(
    languages=["Python"],
    tools=["Bash"],
    mcp_servers=["postgres"],
    topics=["databases"],
    session_count=12,
)


# ── profile helpers ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("mcp__postgres__query", "postgres"),
        ("mcp__github__create_pr", "github"),
        ("Bash", None),
        ("mcp__", None),
        ("", None),
    ],
)
def test_mcp_server_name_extraction(tool_name, expected):
    assert _mcp_server_name(tool_name) == expected


def test_topics_bucket_known_terms():
    topics = _topics_for(["postgres", "Docker", "pytest", "totally-unknown-thing"])
    assert "databases" in topics
    assert "infrastructure" in topics
    assert "testing" in topics


def test_empty_profile_reports_itself_empty():
    assert WorkProfile().is_empty()
    assert not DB_PROFILE.is_empty()


def test_profile_search_signals_include_topics_and_servers():
    signals = DB_PROFILE.search_signals()
    assert "databases" in signals
    assert "postgres" in signals


def test_profile_roundtrips_through_dict():
    restored = WorkProfile.from_dict(DB_PROFILE.to_dict(), session_count=12)
    assert restored.topics == DB_PROFILE.topics
    assert restored.mcp_servers == DB_PROFILE.mcp_servers
    assert restored.session_count == 12


def test_profile_from_dict_tolerates_missing_keys():
    profile = WorkProfile.from_dict({})
    assert profile.is_empty()
    assert profile.languages == []


# ── ranking ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recommendations_match_profile_topics(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="db-migrator", submitter=user, description="Postgres database migrations")
    await _skill(db, name="css-helper", submitter=user, description="Tailwind styling helper")

    results = await recommend_for_user(db, user.id, DB_PROFILE)

    assert results
    assert results[0].candidate.name == "db-migrator"
    assert "database" in results[0].reason.lower() or "postgres" in results[0].reason.lower()


@pytest.mark.asyncio
async def test_recommendations_span_component_types(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="db-skill", submitter=user, description="database work")
    await _mcp(db, name="db-mcp", submitter=user, description="postgres database server")

    results = await recommend_for_user(db, user.id, DB_PROFILE)

    assert {r.candidate.component_type for r in results} == {"skill", "mcp"}


@pytest.mark.asyncio
async def test_already_installed_components_are_excluded(db: AsyncSession):
    user = await _user(db)
    installed = await _skill(db, name="db-installed", submitter=user, description="database migrations")
    await _skill(db, name="db-fresh", submitter=user, description="database migrations")
    await _installed_agent_with(db, user, installed.id)

    results = await recommend_for_user(db, user.id, DB_PROFILE)

    names = [r.candidate.name for r in results]
    assert "db-installed" not in names
    assert "db-fresh" in names


@pytest.mark.asyncio
async def test_dismissed_components_stay_dismissed(db: AsyncSession):
    user = await _user(db)
    unwanted = await _skill(db, name="db-unwanted", submitter=user, description="database migrations")

    before = await recommend_for_user(db, user.id, DB_PROFILE)
    assert "db-unwanted" in [r.candidate.name for r in before]

    await record_feedback(db, user.id, "skill", unwanted.id, "dismissed")
    after = await recommend_for_user(db, user.id, DB_PROFILE)

    assert "db-unwanted" not in [r.candidate.name for r in after]


@pytest.mark.asyncio
async def test_installed_components_stop_being_recommended(db: AsyncSession):
    """ "installed" is terminal feedback, not a no-op.

    Adoption is normally inferred from agent installs, but the API accepts an
    explicit "installed" action and the CLI exposes it. Accepting it and then
    ignoring it would make the flag a lie.
    """
    user = await _user(db)
    taken = await _skill(db, name="db-taken", submitter=user, description="database migrations")

    before = await recommend_for_user(db, user.id, DB_PROFILE)
    assert "db-taken" in [r.candidate.name for r in before]

    await record_feedback(db, user.id, "skill", taken.id, "installed")
    after = await recommend_for_user(db, user.id, DB_PROFILE)

    assert "db-taken" not in [r.candidate.name for r in after]


@pytest.mark.asyncio
async def test_feedback_is_idempotent(db: AsyncSession):
    user = await _user(db)
    skill = await _skill(db, name="db-x", submitter=user, description="database")

    await record_feedback(db, user.id, "skill", skill.id, "dismissed")
    await record_feedback(db, user.id, "skill", skill.id, "not_relevant")

    results = await recommend_for_user(db, user.id, DB_PROFILE)
    assert "db-x" not in [r.candidate.name for r in results]


@pytest.mark.asyncio
async def test_other_users_private_components_are_never_recommended(db: AsyncSession):
    me = await _user(db, "me@example.com")
    them = await _user(db, "them@example.com")
    await _skill(
        db,
        name="their-db-tool",
        submitter=them,
        description="database migrations",
        is_private=True,
    )

    results = await recommend_for_user(db, me.id, DB_PROFILE)

    assert results == []


@pytest.mark.asyncio
async def test_personal_private_components_are_recommended(db: AsyncSession):
    me = await _user(db, "me@example.com")
    await _skill(
        db,
        name="my-db-tool",
        submitter=me,
        description="database migrations",
        is_private=True,
    )

    results = await recommend_for_user(db, me.id, DB_PROFILE)

    assert [r.candidate.name for r in results] == ["my-db-tool"]


@pytest.mark.asyncio
async def test_cold_start_falls_back_to_popularity(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="quiet-thing", submitter=user, description="Something", downloads=1)
    await _skill(db, name="popular-thing", submitter=user, description="Something", downloads=900)

    results = await recommend_for_user(db, user.id, WorkProfile())

    assert results
    assert results[0].candidate.name == "popular-thing"
    # Copy must not claim personalisation that did not happen.
    assert "your work" not in results[0].reason.lower()


@pytest.mark.asyncio
async def test_weak_profile_is_topped_up_with_popular_components(db: AsyncSession):
    """A thin profile must not leave a near-empty rail on a full registry."""
    user = await _user(db)
    await _skill(db, name="db-match", submitter=user, description="postgres database migrations")
    for i in range(5):
        await _skill(db, name=f"unrelated-{i}", submitter=user, description="Styling helpers", downloads=i * 10)

    results = await recommend_for_user(db, user.id, DB_PROFILE, limit=4)

    assert len(results) == 4
    assert results[0].candidate.name == "db-match"
    # The filler must be honest about why it is there.
    assert results[-1].reason == "Popular in your registry"


@pytest.mark.asyncio
async def test_top_up_never_duplicates_a_match(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="db-match", submitter=user, description="postgres database migrations")
    await _skill(db, name="other", submitter=user, description="Styling helpers")

    results = await recommend_for_user(db, user.id, DB_PROFILE, limit=5)

    ids = [r.candidate.id for r in results]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_top_up_respects_dismissals(db: AsyncSession):
    user = await _user(db)
    unwanted = await _skill(db, name="nope", submitter=user, description="Styling helpers")
    await _skill(db, name="fine", submitter=user, description="Styling helpers")
    await record_feedback(db, user.id, "skill", unwanted.id, "dismissed")

    results = await recommend_for_user(db, user.id, DB_PROFILE, limit=5)

    assert "nope" not in [r.candidate.name for r in results]


@pytest.mark.asyncio
async def test_limit_is_respected(db: AsyncSession):
    user = await _user(db)
    for i in range(10):
        await _skill(db, name=f"db-{i}", submitter=user, description="database migrations")

    results = await recommend_for_user(db, user.id, DB_PROFILE, limit=3)

    assert len(results) == 3


@pytest.mark.asyncio
async def test_empty_registry_returns_nothing(db: AsyncSession):
    user = await _user(db)
    assert await recommend_for_user(db, user.id, DB_PROFILE) == []


@pytest.mark.asyncio
async def test_recommendation_dict_shape_is_complete(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="db-migrator", submitter=user, description="database migrations")

    results = await recommend_for_user(db, user.id, DB_PROFILE)
    data = results[0].to_dict()

    for key in ("type", "id", "qualified_name", "description", "latest_version", "reason", "score"):
        assert key in data
