# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Add the cross-product actionable inbox.

Revision ID: 023_inbox
Revises: 022_user_recommendations
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "023_inbox"
down_revision = "022_user_recommendations"
branch_labels = None
depends_on = None


INBOX_KINDS = (
    "review_requested",
    "review_approved",
    "review_rejected",
    "review_comment",
    "change_requested",
    "team_join_requested",
    "team_join_decided",
    "team_created_pending",
    "ownership_transfer",
    "update_available",
    "insight_ready",
    "system_notice",
)

INBOX_STATES = ("open", "done", "dismissed")


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    # Additive and idempotent: a partially applied migration must be safe to
    # re-run, matching the convention used by earlier revisions here.
    bind = op.get_bind()

    # SQLAlchemy's Enum emits a native type on Postgres and a VARCHAR + CHECK on
    # SQLite. create_type=False on Postgres would leave the type missing, so let
    # the dialect decide and only guard against a re-run.
    kind_enum = sa.Enum(*INBOX_KINDS, name="inbox_kind")
    state_enum = sa.Enum(*INBOX_STATES, name="inbox_state")
    if bind.dialect.name == "postgresql":
        kind_enum.create(bind, checkfirst=True)
        state_enum.create(bind, checkfirst=True)
        kind_enum = postgresql.ENUM(*INBOX_KINDS, name="inbox_kind", create_type=False)
        state_enum = postgresql.ENUM(*INBOX_STATES, name="inbox_state", create_type=False)

    if not _has_table("inbox_items"):
        op.create_table(
            "inbox_items",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", kind_enum, nullable=False),
            sa.Column("state", state_enum, nullable=False, server_default="open"),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("action_required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("subject_type", sa.String(length=32), nullable=False),
            sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("subject_namespace", sa.String(length=64), nullable=True),
            sa.Column("subject_slug", sa.String(length=64), nullable=True),
            sa.Column("action_url", sa.String(length=500), nullable=True),
            sa.Column("action_command", sa.String(length=500), nullable=True),
            sa.Column(
                "actor_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "team_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("teams.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("is_private_subject", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("payload", postgresql.JSON(astext_type=sa.Text()), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("user_id", "dedupe_key", name="uq_inbox_items_user_dedupe"),
        )
        op.create_index("ix_inbox_items_user_state_created", "inbox_items", ["user_id", "state", "created_at"])
        op.create_index("ix_inbox_items_user_action_state", "inbox_items", ["user_id", "action_required", "state"])
        op.create_index("ix_inbox_items_user_read", "inbox_items", ["user_id", "read_at"])

    if not _has_table("inbox_item_events"):
        op.create_table(
            "inbox_item_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "item_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("inbox_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event", sa.String(length=32), nullable=False),
            sa.Column(
                "actor_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_inbox_item_events_item", "inbox_item_events", ["item_id"])


def downgrade() -> None:
    if _has_table("inbox_item_events"):
        op.drop_index("ix_inbox_item_events_item", table_name="inbox_item_events")
        op.drop_table("inbox_item_events")
    if _has_table("inbox_items"):
        op.drop_index("ix_inbox_items_user_read", table_name="inbox_items")
        op.drop_index("ix_inbox_items_user_action_state", table_name="inbox_items")
        op.drop_index("ix_inbox_items_user_state_created", table_name="inbox_items")
        op.drop_table("inbox_items")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="inbox_state").drop(bind, checkfirst=True)
        sa.Enum(name="inbox_kind").drop(bind, checkfirst=True)
