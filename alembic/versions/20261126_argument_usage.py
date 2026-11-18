"""Collect value-free argument categories; historical observations stay unavailable."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261126_argument_usage"
down_revision = "20261126_merge_ci_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("observed_requests", sa.Column("argument_usage_version", sa.Integer(), nullable=True))
    op.create_check_constraint("valid_argument_usage_version", "observed_requests",
                               "argument_usage_version IS NULL OR argument_usage_version = 1")
    op.create_table("request_argument_usage",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("argument_name", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("request_id", "field_id", "argument_name"),
        sa.ForeignKeyConstraint(["request_id"], ["observed_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["field_id"], ["schema_fields.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("state IN ('omitted', 'explicit_null', 'provided', 'unknown', 'mixed')",
                           name="valid_argument_usage_state"))
    op.create_index("ix_argument_usage_field_argument_state", "request_argument_usage",
                    ["field_id", "argument_name", "state"])


def downgrade():
    op.drop_table("request_argument_usage")
    op.drop_constraint("valid_argument_usage_version", "observed_requests", type_="check")
    op.drop_column("observed_requests", "argument_usage_version")
