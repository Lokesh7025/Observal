# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for registry-aware insight suggestions.

The validation gate is the load-bearing piece: an id the model invented must
never reach the UI as a real component link.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import services.dynamic_settings as ds
from models.base import Base
from models.mcp import ListingStatus
from models.skill import SkillListing, SkillVersion
from models.user import User
from services.insights.registry_match import (
    CatalogOffer,
    RegistryScope,
    build_catalog,
    build_signals,
    catalog_block,
    count_reused,
    validate_reuse_suggestions,
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


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Keep each test's setting overrides from leaking into the next."""
    original = dict(ds._sync_cache)
    yield
    ds._sync_cache = original


async def _user(db: AsyncSession, email: str = "a@example.com") -> User:
    user = User(email=email, username=email.split("@")[0], name=email)
    db.add(user)
    await db.flush()
    return user


async def _skill(
    db: AsyncSession,
    *,
    name: str,
    submitter: User,
    description: str = "Database migrations",
    status: ListingStatus = ListingStatus.approved,
    is_private: bool = False,
) -> SkillListing:
    listing = SkillListing(
        name=name,
        namespace="test",
        slug=name,
        owner="tester",
        submitted_by=submitter.id,
        is_private=is_private,
    )
    db.add(listing)
    await db.flush()
    version = SkillVersion(
        listing_id=listing.id,
        version="1.5.0",
        description=description,
        status=status,
        task_type="general",
        delivery_mode="registry_direct",
        released_by=submitter.id,
        released_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()
    listing.latest_version_id = version.id
    await db.flush()
    return listing


def _narrative(features: list[dict]) -> dict:
    return {"suggestions": {"features_to_try": features}}


# ── signal building ───────────────────────────────────────────────────────


def test_build_signals_handles_mixed_aggregate_shapes():
    signals = build_signals(
        agg={
            "top_languages": [["Python", 12], ["TypeScript", 4]],
            "top_tools": [{"tool": "Bash"}, {"name": "Edit"}],
            "tool_error_categories": {"permission_denied": 3},
        },
        facets_summary={
            "goal_categories": [{"category": "database migrations"}],
            "friction_types": [{"type": "wrong_approach"}],
            "repeated_instructions": [{"instruction": "run ruff before finishing"}],
        },
    )

    for expected in ("Python", "Bash", "Edit", "database migrations", "wrong_approach", "permission_denied"):
        assert expected in signals


def test_build_signals_survives_empty_input():
    assert build_signals() == ""
    assert build_signals(agg={}, facets_summary={}, agent_config={}) == ""


def test_build_signals_extracts_mcp_server_names():
    """`mcp__postgres__query` is one opaque token; the server name is the signal."""
    signals = build_signals(agg={"top_tools": [["mcp__postgres__query", 40], ["Bash", 10]]})

    assert "postgres" in signals.split()
    assert "mcp__postgres__query" in signals


def test_build_signals_includes_configured_mcps():
    signals = build_signals(agent_config={"configured_mcps": ["github-mcp"]})
    assert "github-mcp" in signals


def test_build_signals_carries_domain_words_from_the_prompt():
    """Tool names describe mechanics; the prompt is where the domain lives."""
    signals = build_signals(
        agg={"top_tools": [["Edit", 20], ["Read", 15]], "top_languages": [["TypeScript", 30]]},
        agent_config={"system_prompt_excerpt": "You help build the React dashboard front end."},
    )

    lowered = signals.lower()
    assert "react" in lowered
    assert "dashboard" in lowered


def test_build_signals_truncates_long_prompts():
    signals = build_signals(agent_config={"system_prompt_excerpt": "x" * 5000})
    assert len(signals) < 1000


# ── catalog building ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_catalog_returns_matches(db: AsyncSession):
    user = await _user(db)
    skill = await _skill(db, name="db-migrator", submitter=user)

    offer = await build_catalog(db, RegistryScope(), "database migrations")

    assert offer.item_count == 1
    assert skill.id in offer.offered_ids
    assert offer.entries_by_type["skills"][0]["qualified_name"] == "test/db-migrator"


@pytest.mark.asyncio
async def test_build_catalog_excludes_already_attached(db: AsyncSession):
    user = await _user(db)
    attached = await _skill(db, name="db-attached", submitter=user)
    await _skill(db, name="db-other", submitter=user)

    offer = await build_catalog(db, RegistryScope(attached_ids=(attached.id,)), "database")

    assert attached.id not in offer.offered_ids
    assert offer.item_count == 1


@pytest.mark.asyncio
async def test_build_catalog_respects_kill_switch(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="db-migrator", submitter=user)
    ds._sync_cache["insights.registry_match_enabled"] = "false"

    offer = await build_catalog(db, RegistryScope(), "database migrations")

    assert not offer
    assert offer.item_count == 0


@pytest.mark.asyncio
async def test_build_catalog_respects_item_cap(db: AsyncSession):
    user = await _user(db)
    for i in range(8):
        await _skill(db, name=f"db-{i}", submitter=user)
    ds._sync_cache["insights.registry_match_max_items"] = "3"

    offer = await build_catalog(db, RegistryScope(), "database")

    assert offer.item_count == 3


@pytest.mark.asyncio
async def test_build_catalog_excludes_other_user_private(db: AsyncSession):
    owner = await _user(db, "owner@example.com")
    outsider = await _user(db, "outsider@example.com")
    await _skill(db, name="their-db-tool", submitter=outsider, is_private=True)

    offer = await build_catalog(db, RegistryScope(user_id=owner.id), "database")

    assert offer.item_count == 0


@pytest.mark.asyncio
async def test_empty_registry_is_distinguished_from_no_match(db: AsyncSession):
    """The report says different things for these, so they must differ here."""
    offer = await build_catalog(db, RegistryScope(), "database migrations")

    assert offer.item_count == 0
    assert offer.registry_has_components is False


@pytest.mark.asyncio
async def test_no_match_against_a_populated_registry(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="css-helper", submitter=user, description="Tailwind styling helper")

    offer = await build_catalog(db, RegistryScope(), "kubernetes ingress certificates")

    assert offer.item_count == 0
    # Components exist, they just did not fit.
    assert offer.registry_has_components is True


@pytest.mark.asyncio
async def test_successful_match_skips_the_probe(db: AsyncSession):
    user = await _user(db)
    await _skill(db, name="db-migrator", submitter=user)

    offer = await build_catalog(db, RegistryScope(), "database migrations")

    assert offer.item_count == 1
    # No need to ask whether the registry is populated; we found something.
    assert offer.registry_has_components is None


@pytest.mark.asyncio
async def test_disabled_feature_is_reported_as_disabled(db: AsyncSession):
    ds._sync_cache["insights.registry_match_enabled"] = "false"

    offer = await build_catalog(db, RegistryScope(), "database")

    assert offer.enabled is False
    assert offer.to_summary()["enabled"] is False


def test_summary_reports_reuse_count():
    offer = CatalogOffer(entries_by_type={"skills": [{"id": "a"}, {"id": "b"}]})

    summary = offer.to_summary(reused=1)

    assert summary == {
        "enabled": True,
        "offered": 2,
        "reused": 1,
        "registry_has_components": None,
    }


def test_count_reused_only_counts_validated_refs():
    narrative = {
        "suggestions": {
            "features_to_try": [
                {"component_ref": {"id": "x"}},
                {"component_ref": None},
                {"action_type": "create_new_skill"},
                "not a dict",
            ]
        }
    }

    assert count_reused(narrative) == 1


@pytest.mark.parametrize(
    "narrative",
    [{}, {"suggestions": None}, {"suggestions": {"features_to_try": "nope"}}],
)
def test_count_reused_tolerates_malformed(narrative):
    assert count_reused(narrative) == 0


def test_catalog_block_is_empty_without_offer():
    assert catalog_block(CatalogOffer()) == ""


def test_catalog_block_instructs_verbatim_ids():
    offer = CatalogOffer(entries_by_type={"skills": [{"id": "x", "name": "y"}]}, offered_ids={uuid.uuid4()})
    block = catalog_block(offer)
    assert "never invent an id" in block.lower()


# ── validation gate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_reuse_is_kept_and_enriched(db: AsyncSession):
    user = await _user(db)
    skill = await _skill(db, name="db-migrator", submitter=user)
    offer = CatalogOffer(offered_ids={skill.id})
    narrative = _narrative(
        [
            {
                "action_type": "reuse_existing_component",
                "feature": "Skill",
                "existing_component_id": str(skill.id),
            }
        ]
    )

    result = await validate_reuse_suggestions(narrative, offer, db, RegistryScope())

    feature = result["suggestions"]["features_to_try"][0]
    assert feature["action_type"] == "reuse_existing_component"
    assert feature["component_ref"]["qualified_name"] == "test/db-migrator"
    assert feature["component_ref"]["latest_version"] == "1.5.0"


@pytest.mark.asyncio
async def test_hallucinated_id_is_stripped(db: AsyncSession):
    """An id that was never offered must not survive, however plausible."""
    await _user(db)
    offer = CatalogOffer(offered_ids=set())
    narrative = _narrative(
        [
            {
                "action_type": "reuse_existing_component",
                "feature": "Skill",
                "existing_component_id": str(uuid.uuid4()),
                "one_liner": "Invented",
            }
        ]
    )

    result = await validate_reuse_suggestions(narrative, offer, db, RegistryScope())

    feature = result["suggestions"]["features_to_try"][0]
    assert feature["existing_component_id"] is None
    assert feature["component_ref"] is None
    assert feature["action_type"] != "reuse_existing_component"
    # The idea survives even though the fake reference does not.
    assert feature["one_liner"] == "Invented"


@pytest.mark.asyncio
async def test_offered_but_unresolvable_id_is_stripped(db: AsyncSession):
    """Offered ids still get re-checked: the listing may have been archived."""
    user = await _user(db)
    pending = await _skill(db, name="not-approved", submitter=user, status=ListingStatus.pending)
    offer = CatalogOffer(offered_ids={pending.id})
    narrative = _narrative(
        [
            {
                "action_type": "reuse_existing_component",
                "feature": "Skill",
                "existing_component_id": str(pending.id),
            }
        ]
    )

    result = await validate_reuse_suggestions(narrative, offer, db, RegistryScope())

    assert result["suggestions"]["features_to_try"][0]["component_ref"] is None


@pytest.mark.asyncio
async def test_reuse_across_user_boundary_is_stripped(db: AsyncSession):
    owner = await _user(db, "owner@example.com")
    outsider = await _user(db, "outsider@example.com")
    foreign = await _skill(db, name="theirs", submitter=outsider, is_private=True)
    # Even if the id somehow ends up in the offer, resolution is visibility-scoped.
    offer = CatalogOffer(offered_ids={foreign.id})
    narrative = _narrative(
        [
            {
                "action_type": "reuse_existing_component",
                "feature": "Skill",
                "existing_component_id": str(foreign.id),
            }
        ]
    )

    result = await validate_reuse_suggestions(narrative, offer, db, RegistryScope(user_id=owner.id))

    assert result["suggestions"]["features_to_try"][0]["component_ref"] is None


@pytest.mark.asyncio
async def test_create_new_suggestions_are_untouched(db: AsyncSession):
    await _user(db)
    narrative = _narrative(
        [
            {
                "action_type": "create_new_skill",
                "feature": "Skill",
                "name": "scope-guard",
                "example": "---\nname: scope-guard\n---\nSteps",
            }
        ]
    )

    result = await validate_reuse_suggestions(narrative, CatalogOffer(), db, RegistryScope())

    feature = result["suggestions"]["features_to_try"][0]
    assert feature["action_type"] == "create_new_skill"
    assert "component_ref" not in feature


@pytest.mark.asyncio
async def test_non_uuid_reference_is_ignored(db: AsyncSession):
    await _user(db)
    narrative = _narrative(
        [
            {
                "action_type": "reuse_existing_component",
                "feature": "Skill",
                "existing_component_id": "registry/db-migrator",
            }
        ]
    )

    result = await validate_reuse_suggestions(narrative, CatalogOffer(), db, RegistryScope())

    # Not a uuid, so there is nothing to validate or enrich.
    assert result["suggestions"]["features_to_try"][0].get("component_ref") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "narrative",
    [
        {},
        {"suggestions": None},
        {"suggestions": {}},
        {"suggestions": {"features_to_try": None}},
        {"suggestions": {"features_to_try": []}},
        {"suggestions": {"features_to_try": ["not a dict"]}},
        {"suggestions": "a string"},
    ],
)
async def test_validation_tolerates_malformed_narratives(db: AsyncSession, narrative):
    """Old reports and partial LLM output must not raise."""
    result = await validate_reuse_suggestions(narrative, CatalogOffer(), db, RegistryScope())
    assert result is narrative
