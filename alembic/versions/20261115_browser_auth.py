"""Add server-side browser sessions, users, service ACL and login throttling.

Revision ID: 20261115_auth
Revises: 20261114_visibility
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261115_auth"
down_revision = "20261114_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('viewer', 'analyst', 'operator', 'admin')", name="valid_role"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "auth_service_grants",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("auth_users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("graphql_services.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_auth_service_grants_service_id", "auth_service_grants", ["service_id"])
    op.create_table(
        "auth_login_throttles",
        sa.Column("bucket_key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
    )
    op.create_index("ix_auth_login_throttles_expires_at", "auth_login_throttles", ["expires_at"])


def downgrade() -> None:
    op.drop_table("auth_login_throttles")
    op.drop_table("auth_service_grants")
    op.drop_table("auth_sessions")
    op.drop_table("auth_users")
