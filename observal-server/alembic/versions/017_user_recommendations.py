# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Add per-user work profiles and recommendation feedback.

Revision ID: 017_user_recommendations
Revises: 016_registry_publish_loop
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "017_user_recommendations"
down_revision = "016_registry_publish_loop"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    # Additive and idempotent: a partially applied migration must be safe to
    # re-run, matching the convention used by earlier revisions here.
    if not _has_table("user_work_profiles"):
        op.create_table(
            "user_work_profiles",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("session_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("profile", postgresql.JSON(astext_type=sa.Text()), nullable=False),
            sa.UniqueConstraint("user_id", name="uq_user_work_profiles_user"),
        )

    if not _has_table("recommendation_feedback"):
        op.create_table(
            "recommendation_feedback",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("component_type", sa.String(length=50), nullable=False),
            sa.Column("component_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("action", sa.String(length=30), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "user_id",
                "component_type",
                "component_id",
                name="uq_recommendation_feedback_user_component",
            ),
        )
        op.create_index("ix_recommendation_feedback_user", "recommendation_feedback", ["user_id"])


def downgrade() -> None:
    if _has_table("recommendation_feedback"):
        op.drop_index("ix_recommendation_feedback_user", table_name="recommendation_feedback")
        op.drop_table("recommendation_feedback")
    if _has_table("user_work_profiles"):
        op.drop_table("user_work_profiles")
