"""Introduce opt-in public visibility for GraphQL services.

Revision ID: 20261114_visibility
Revises: 20261113_initial
Create Date: 2026-11-14
"""

from alembic import op
import sqlalchemy as sa


revision = "20261114_visibility"
down_revision = "20261113_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing services are private until an administrator publishes them.
    op.add_column(
        "graphql_services",
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("graphql_services", "is_public")
