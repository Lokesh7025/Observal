# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Rename the ``clickhouse`` migration scope to ``telemetry``.

Revision ID: 027_telemetry_migration_scope
Revises: 026_usage_ping_state
"""

from alembic import op

revision = "027_telemetry_migration_scope"
down_revision = "026_usage_ping_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TYPE migration_scope RENAME VALUE 'clickhouse' TO 'telemetry'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TYPE migration_scope RENAME VALUE 'telemetry' TO 'clickhouse'")
