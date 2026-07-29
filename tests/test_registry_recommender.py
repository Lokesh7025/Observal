# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared registry recommender.

Covers the three things every caller depends on: visibility never leaks
across orgs, ranking puts relevance ahead of popularity, and reference
resolution rejects anything unapproved, invisible, or mistyped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.hook import HookListing, HookVersion
from models.mcp import ListingStatus, McpListing, McpVersion
from models.organization import Organization
from models.prompt import PromptListing, PromptVersion
from models.skill import SkillListing, SkillVersion
from models.user import User
from services.registry_recommender import (
    ComponentCandidate,
    build_signal_query,
    coerce_uuid,
    resolve_component_any_type,
    resolve_components,
    shortlist,
    visibility_clause,
)


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


async def _user(db: AsyncSession, email: str, org_id: uuid.UUID | None = None) -> User:
    user = User(email=email, username=email.split("@")[0], name=email, org_id=org_id)
    db.add(user)
    await db.flush()
    return user


async def _org(db: AsyncSession, slug: str) -> Organization:
    org = Organization(name=slug.title(), slug=slug)
    db.add(org)
    await db.flush()
    return org


async def _skill(
    db: AsyncSession,
    *,
    name: str,
    description: str,
    submitter: User,
    status: ListingStatus = ListingStatus.approved,
    is_private: bool = False,
    org_id: uuid.UUID | None = None,
    downloads: int = 0,
    task_type: str = "general",
) -> SkillListing:
    listing = SkillListing(
        name=name,
        namespace="test",
        slug=name.lower().replace(" ", "-"),
        owner="tester",
        submitted_by=submitter.id,
        is_private=is_private,
        owner_org_id=org_id,
    )
    db.add(listing)
    await db.flush()
    version = SkillVersion(
        listing_id=listing.id,
        version="2.3.1",
        description=description,
        status=status,
        task_type=task_type,
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


async def _hook(db: AsyncSession, *, name: str, description: str, submitter: User) -> HookListing:
    listing = HookListing(
        name=name,
        namespace="test",
        slug=name.lower().replace(" ", "-"),
        owner="tester",
        submitted_by=submitter.id,
    )
    db.add(listing)
    await db.flush()
    version = HookVersion(
        listing_id=listing.id,
        version="1.0.0",
        description=description,
        status=ListingStatus.approved,
        event="PreToolUse",
        handler_type="script",
        released_by=submitter.id,
        released_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()
    listing.latest_version_id = version.id
    await db.flush()
    return listing


async def _mcp(
    db: AsyncSession, *, name: str, description: str, submitter: User, category: str = "databases"
) -> McpListing:
    listing = McpListing(
        name=name,
        namespace="test",
        slug=name.lower().replace(" ", "-"),
        owner="tester",
        category=category,
        submitted_by=submitter.id,
    )
    db.add(listing)
    await db.flush()
    version = McpVersion(
        listing_id=listing.id,
        version="1.4.0",
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


async def _prompt(db: AsyncSession, *, name: str, description: str, submitter: User) -> PromptListing:
    listing = PromptListing(
        name=name,
        namespace="test",
        slug=name.lower().replace(" ", "-"),
        owner="tester",
        submitted_by=submitter.id,
    )
    db.add(listing)
    await db.flush()
    version = PromptVersion(
        listing_id=listing.id,
        version="1.0.0",
        description=description,
        status=ListingStatus.approved,
        category="general",
        template="do the thing",
        released_by=submitter.id,
        released_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()
    listing.latest_version_id = version.id
    await db.flush()
    return listing


# ── visibility ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shortlist_returns_public_components(db: AsyncSession):
    user = await _user(db, "a@example.com")
    await _skill(db, name="db-migrator", description="Run database migrations safely", submitter=user)

    results = await shortlist(db, signals="database migrations", component_types=["skill"])

    assert [c.name for c in results] == ["db-migrator"]
    assert results[0].qualified_name == "test/db-migrator"
    assert results[0].latest_version == "2.3.1"


@pytest.mark.asyncio
async def test_shortlist_hides_private_components_from_other_orgs(db: AsyncSession):
    org_a = await _org(db, "org-a")
    org_b = await _org(db, "org-b")
    owner = await _user(db, "owner@a.com", org_id=org_a.id)
    await _skill(
        db,
        name="secret-db-tool",
        description="Internal database tooling",
        submitter=owner,
        is_private=True,
        org_id=org_a.id,
    )

    same_org = await shortlist(db, signals="database", component_types=["skill"], org_id=org_a.id)
    other_org = await shortlist(db, signals="database", component_types=["skill"], org_id=org_b.id)
    no_org = await shortlist(db, signals="database", component_types=["skill"], org_id=None)

    assert [c.name for c in same_org] == ["secret-db-tool"]
    assert other_org == []
    assert no_org == []


@pytest.mark.asyncio
async def test_shortlist_shows_private_component_to_its_submitter(db: AsyncSession):
    org = await _org(db, "org-a")
    owner = await _user(db, "owner@a.com", org_id=org.id)
    await _skill(
        db,
        name="my-db-tool",
        description="Personal database helper",
        submitter=owner,
        is_private=True,
        org_id=org.id,
    )

    results = await shortlist(db, signals="database", component_types=["skill"], org_id=None, user_id=owner.id)

    assert [c.name for c in results] == ["my-db-tool"]


@pytest.mark.asyncio
async def test_visibility_clause_returns_none_for_models_without_privacy():
    class Bare:
        pass

    assert visibility_clause(Bare, uuid.uuid4()) is None


# ── filtering ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shortlist_skips_unapproved_versions(db: AsyncSession):
    user = await _user(db, "a@example.com")
    await _skill(
        db,
        name="pending-db-tool",
        description="Database tooling",
        submitter=user,
        status=ListingStatus.pending,
    )

    assert await shortlist(db, signals="database", component_types=["skill"]) == []


@pytest.mark.asyncio
async def test_shortlist_skips_listing_without_latest_version(db: AsyncSession):
    user = await _user(db, "a@example.com")
    listing = SkillListing(
        name="orphan-db",
        namespace="test",
        slug="orphan-db",
        owner="tester",
        submitted_by=user.id,
    )
    db.add(listing)
    await db.flush()

    assert await shortlist(db, signals="database", component_types=["skill"]) == []


@pytest.mark.asyncio
async def test_shortlist_honours_exclude_ids(db: AsyncSession):
    user = await _user(db, "a@example.com")
    attached = await _skill(db, name="db-attached", description="Database work", submitter=user)
    await _skill(db, name="db-available", description="Database work", submitter=user)

    results = await shortlist(db, signals="database", component_types=["skill"], exclude_ids=[attached.id])

    assert [c.name for c in results] == ["db-available"]


@pytest.mark.asyncio
async def test_shortlist_caps_results_per_type_and_total(db: AsyncSession):
    user = await _user(db, "a@example.com")
    for i in range(10):
        await _skill(db, name=f"db-skill-{i}", description="Database tooling", submitter=user)

    results = await shortlist(
        db,
        signals="database",
        component_types=["skill"],
        per_type_limit=3,
        total_limit=2,
    )

    assert len(results) == 2


# ── ranking ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relevance_outranks_popularity(db: AsyncSession):
    user = await _user(db, "a@example.com")
    await _skill(
        db,
        name="popular-linter",
        description="Lint your code",
        submitter=user,
        downloads=100_000,
    )
    await _skill(
        db,
        name="postgres-migration-runner",
        description="Run postgres database migrations",
        submitter=user,
        downloads=0,
    )

    results = await shortlist(db, signals="postgres database migrations", component_types=["skill"])

    assert results[0].name == "postgres-migration-runner"


@pytest.mark.asyncio
async def test_popularity_breaks_ties_between_equally_relevant(db: AsyncSession):
    user = await _user(db, "a@example.com")
    await _skill(db, name="db-tool-quiet", description="Database tooling", submitter=user, downloads=0)
    await _skill(db, name="db-tool-loud", description="Database tooling", submitter=user, downloads=500)

    results = await shortlist(db, signals="database tooling", component_types=["skill"])

    assert results[0].name == "db-tool-loud"


@pytest.mark.asyncio
async def test_empty_signals_fall_back_to_popularity(db: AsyncSession):
    user = await _user(db, "a@example.com")
    await _skill(db, name="quiet", description="Something", submitter=user, downloads=1)
    await _skill(db, name="loud", description="Something", submitter=user, downloads=99)

    # "the" and "a" are stop words, so no usable tokens survive.
    results = await shortlist(db, signals="the a", component_types=["skill"])

    assert [c.name for c in results] == ["loud", "quiet"]


@pytest.mark.asyncio
async def test_shortlist_spans_multiple_component_types(db: AsyncSession):
    user = await _user(db, "a@example.com")
    await _skill(db, name="db-skill", description="Database migrations", submitter=user)
    await _mcp(db, name="db-mcp", description="Database access server", submitter=user)
    await _prompt(db, name="db-prompt", description="Database review prompt", submitter=user)

    results = await shortlist(db, signals="database", component_types=["skill", "mcp", "prompt"])

    assert {c.component_type for c in results} == {"skill", "mcp", "prompt"}


@pytest.mark.asyncio
async def test_matched_terms_are_recorded(db: AsyncSession):
    user = await _user(db, "a@example.com")
    await _skill(db, name="db-migrator", description="Handles postgres migrations", submitter=user)

    results = await shortlist(db, signals="postgres migration", component_types=["skill"])

    assert "postgres" in results[0].matched_terms


# ── reference resolution ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_components_returns_validated_reference(db: AsyncSession):
    user = await _user(db, "a@example.com")
    skill = await _skill(db, name="db-migrator", description="Database migrations", submitter=user)

    resolved = await resolve_components(db, [("skill", skill.id)])

    hit = resolved[("skill", skill.id)]
    assert hit.qualified_name == "test/db-migrator"
    assert hit.latest_version == "2.3.1"
    assert hit.to_ref()["type"] == "skill"


@pytest.mark.asyncio
async def test_resolve_components_rejects_wrong_type(db: AsyncSession):
    user = await _user(db, "a@example.com")
    skill = await _skill(db, name="db-migrator", description="Database migrations", submitter=user)

    resolved = await resolve_components(db, [("hook", skill.id)])

    assert resolved == {}


@pytest.mark.asyncio
async def test_resolve_components_rejects_unapproved(db: AsyncSession):
    user = await _user(db, "a@example.com")
    skill = await _skill(db, name="db-migrator", description="Database", submitter=user, status=ListingStatus.pending)

    assert await resolve_components(db, [("skill", skill.id)]) == {}


@pytest.mark.asyncio
async def test_resolve_components_rejects_other_org_private(db: AsyncSession):
    org_a = await _org(db, "org-a")
    org_b = await _org(db, "org-b")
    owner = await _user(db, "owner@a.com", org_id=org_a.id)
    skill = await _skill(
        db,
        name="secret",
        description="Private",
        submitter=owner,
        is_private=True,
        org_id=org_a.id,
    )

    assert await resolve_components(db, [("skill", skill.id)], org_id=org_b.id) == {}
    assert await resolve_components(db, [("skill", skill.id)], org_id=org_a.id) != {}


@pytest.mark.asyncio
async def test_resolve_components_ignores_unknown_type(db: AsyncSession):
    assert await resolve_components(db, [("nonsense", uuid.uuid4())]) == {}


@pytest.mark.asyncio
async def test_resolve_component_any_type_finds_correct_type(db: AsyncSession):
    user = await _user(db, "a@example.com")
    hook = await _hook(db, name="guard", description="Scope guard", submitter=user)

    hit = await resolve_component_any_type(db, hook.id)

    assert hit is not None
    assert hit.component_type == "hook"


@pytest.mark.asyncio
async def test_resolve_component_any_type_returns_none_for_unknown_id(db: AsyncSession):
    assert await resolve_component_any_type(db, uuid.uuid4()) is None


# ── helpers ───────────────────────────────────────────────────────────────


def test_coerce_uuid_accepts_valid_forms():
    value = uuid.uuid4()
    assert coerce_uuid(value) == value
    assert coerce_uuid(str(value)) == value
    assert coerce_uuid(f"  {value}  ") == value


@pytest.mark.parametrize("bad", ["", None, "not-a-uuid", "registry/skill-name", 42, [], {}])
def test_coerce_uuid_rejects_junk(bad):
    assert coerce_uuid(bad) is None


def test_build_signal_query_flattens_and_dedupes():
    query = build_signal_query("database", ["Database", "migrations"], None, "", ["testing"])
    assert query == "database migrations testing"


def test_catalog_entry_stays_compact():
    candidate = ComponentCandidate(
        component_type="skill",
        id=uuid.uuid4(),
        name="db-migrator",
        namespace="test",
        slug="db-migrator",
        description="x" * 500,
        latest_version="1.0.0",
        download_count=3,
        score=1.0,
        matched_terms=["database"],
    )

    entry = candidate.to_catalog_entry()

    assert len(entry["description"]) == 160
    assert entry["qualified_name"] == "test/db-migrator"
    assert entry["matched_on"] == ["database"]
