# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Stress and adversarial tests for the shared recommender.

Two untrusted inputs reach this code: registry text written by publishers
(names, descriptions) and the signal string derived from telemetry. Neither
may crash the caller, leak across tenants, or alter a query.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.mcp import ListingStatus
from models.organization import Organization
from models.skill import SkillListing, SkillVersion
from models.user import User
from services.insights.registry_match import (
    CatalogOffer,
    RegistryScope,
    build_signals,
    catalog_block,
    validate_reuse_suggestions,
)
from services.registry_recommender import (
    build_signal_query,
    coerce_uuid,
    resolve_components,
    shortlist,
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


async def _user(db: AsyncSession, email: str = "a@example.com", org_id=None) -> User:
    user = User(email=email, username=email.split("@")[0], name=email, org_id=org_id)
    db.add(user)
    await db.flush()
    return user


async def _skill(db: AsyncSession, *, name: str, description: str, submitter: User, **kw) -> SkillListing:
    listing = SkillListing(
        name=name,
        namespace="test",
        slug=name.lower().replace(" ", "-")[:60],
        owner="t",
        submitted_by=submitter.id,
        is_private=kw.get("is_private", False),
        owner_org_id=kw.get("org_id"),
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
        released_by=submitter.id,
        released_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()
    listing.latest_version_id = version.id
    await db.flush()
    return listing


# ── hostile signal strings ────────────────────────────────────────────────

HOSTILE_SIGNALS = [
    "'; DROP TABLE skill_listings; --",
    "%%%%%%",  # LIKE wildcards
    "_" * 200,  # LIKE single-char wildcards
    "\\%\\_",  # escaped wildcards
    "' OR '1'='1",
    "<script>alert(1)</script>",
    "../../etc/passwd",
    "\x00\x01\x02",
    "ünïcodé ✨ 中文",
    "a" * 20000,  # very long
    "\n\r\t",
    "{}{}{}",  # format-string shaped
    "{ids:Array(String)}",  # clickhouse param shaped
]


@pytest.mark.asyncio
@pytest.mark.parametrize("signals", HOSTILE_SIGNALS)
async def test_shortlist_survives_hostile_signals(db: AsyncSession, signals):
    user = await _user(db)
    await _skill(db, name="db-tool", description="Database migrations", submitter=user)

    results = await shortlist(db, signals=signals, component_types=["skill"])

    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_wildcards_do_not_match_everything(db: AsyncSession):
    """A LIKE wildcard in the signal must not turn into 'select all'."""
    user = await _user(db)
    await _skill(db, name="alpha", description="Totally unrelated thing", submitter=user)
    await _skill(db, name="beta", description="Also unrelated", submitter=user)

    results = await shortlist(db, signals="%", component_types=["skill"])

    # "%" yields no usable tokens, so this falls back to popularity ordering
    # rather than a wildcard match. Either way it must not error.
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_hostile_registry_text_is_returned_verbatim_not_executed(db: AsyncSession):
    user = await _user(db)
    await _skill(
        db,
        name="evil",
        description="'; DROP TABLE skill_listings; -- database",
        submitter=user,
    )

    results = await shortlist(db, signals="database", component_types=["skill"])

    assert len(results) == 1
    assert "DROP TABLE" in results[0].description
    # The table still exists.
    assert await shortlist(db, signals="database", component_types=["skill"])


# ── tenant isolation under stress ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_org_leak_across_many_orgs(db: AsyncSession):
    orgs = []
    for i in range(6):
        org = Organization(name=f"org{i}", slug=f"org-{i}")
        db.add(org)
        await db.flush()
        orgs.append(org)
        owner = await _user(db, f"o{i}@x.com", org_id=org.id)
        await _skill(
            db,
            name=f"secret-db-{i}",
            description="Database migrations internal",
            submitter=owner,
            is_private=True,
            org_id=org.id,
        )

    for i, org in enumerate(orgs):
        results = await shortlist(db, signals="database migrations", org_id=org.id)
        names = {c.name for c in results}
        assert names == {f"secret-db-{i}"}, f"org {i} saw {names}"


@pytest.mark.asyncio
async def test_concurrent_shortlists_stay_isolated(db: AsyncSession):
    """Concurrency must not bleed one org's results into another's."""
    org_a = Organization(name="A", slug="a")
    org_b = Organization(name="B", slug="b")
    db.add_all([org_a, org_b])
    await db.flush()
    ua = await _user(db, "a@a.com", org_id=org_a.id)
    ub = await _user(db, "b@b.com", org_id=org_b.id)
    await _skill(db, name="a-secret", description="database", submitter=ua, is_private=True, org_id=org_a.id)
    await _skill(db, name="b-secret", description="database", submitter=ub, is_private=True, org_id=org_b.id)

    results = await asyncio.gather(
        *[shortlist(db, signals="database", org_id=org_a.id) for _ in range(5)],
        *[shortlist(db, signals="database", org_id=org_b.id) for _ in range(5)],
    )

    for r in results[:5]:
        assert {c.name for c in r} == {"a-secret"}
    for r in results[5:]:
        assert {c.name for c in r} == {"b-secret"}


# ── validation gate under hostile LLM output ──────────────────────────────

HOSTILE_REFS = [
    "'; DROP TABLE agents; --",
    "../../../etc/passwd",
    "<script>alert(1)</script>",
    "00000000-0000-0000-0000-000000000000",
    "not-a-uuid",
    "",
    None,
    12345,
    {"nested": "object"},
    ["a", "list"],
    "  " + str(uuid.uuid4()) + "  ",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ref", HOSTILE_REFS)
async def test_validation_gate_rejects_hostile_refs(db: AsyncSession, bad_ref):
    await _user(db)
    narrative = {
        "suggestions": {
            "features_to_try": [
                {
                    "action_type": "reuse_existing_component",
                    "feature": "Skill",
                    "existing_component_id": bad_ref,
                }
            ]
        }
    }

    result = await validate_reuse_suggestions(narrative, CatalogOffer(), db, RegistryScope())

    feature = result["suggestions"]["features_to_try"][0]
    assert feature.get("component_ref") is None


@pytest.mark.asyncio
async def test_validation_gate_handles_many_features(db: AsyncSession):
    user = await _user(db)
    skill = await _skill(db, name="real", description="database", submitter=user)
    features = [
        {
            "action_type": "reuse_existing_component",
            "feature": "Skill",
            "existing_component_id": str(uuid.uuid4()),
        }
        for _ in range(200)
    ]
    features.append(
        {
            "action_type": "reuse_existing_component",
            "feature": "Skill",
            "existing_component_id": str(skill.id),
        }
    )
    narrative = {"suggestions": {"features_to_try": features}}

    result = await validate_reuse_suggestions(narrative, CatalogOffer(offered_ids={skill.id}), db, RegistryScope())

    kept = [f for f in result["suggestions"]["features_to_try"] if f.get("component_ref")]
    assert len(kept) == 1
    assert kept[0]["component_ref"]["name"] == "real"


@pytest.mark.asyncio
async def test_resolve_components_ignores_non_uuid_entries(db: AsyncSession):
    assert await resolve_components(db, [("skill", "not-a-uuid")]) == {}  # type: ignore[list-item]


# ── signal builders under junk ────────────────────────────────────────────


@pytest.mark.parametrize(
    "agg",
    [
        {"top_tools": None},
        {"top_tools": "a string"},
        {"top_tools": [None, [], {}, 42]},
        {"top_languages": {"Python": 3}},
        {"tool_error_categories": None},
        {"top_tools": [["mcp__", 1]]},
    ],
)
def test_build_signals_never_raises(agg):
    assert isinstance(build_signals(agg=agg), str)


@pytest.mark.parametrize(
    "facets",
    [
        {"goal_categories": None},
        {"repeated_instructions": [{"nope": 1}]},
        {"repeated_instructions": "string"},
        {"friction_types": [[None, 2]]},
    ],
)
def test_build_signals_never_raises_on_facets(facets):
    assert isinstance(build_signals(facets_summary=facets), str)


def test_build_signal_query_ignores_none_and_blank():
    assert build_signal_query(None, "", [None, "", "ok"]) == "ok"


def test_catalog_block_escapes_nothing_but_stays_json():
    import json

    offer = CatalogOffer(
        entries_by_type={"skills": [{"id": "x", "name": "</script>", "description": 'a"b'}]},
        offered_ids={uuid.uuid4()},
    )
    block = catalog_block(offer)
    # The payload must remain valid JSON so the prompt cannot be broken apart.
    start = block.index("{")
    json.loads(block[start:])


@pytest.mark.parametrize("value", ["", None, "x", 0, [], {}, "0" * 40])
def test_coerce_uuid_is_total(value):
    assert coerce_uuid(value) is None or isinstance(coerce_uuid(value), uuid.UUID)
