"""Add per-request presence evidence without backfilling historical responses."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261122_field_evidence"
down_revision = "20261121_gitlab_pipelines"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("observed_requests", sa.Column("field_evidence_version", sa.Integer(), nullable=True))
    op.create_check_constraint("valid_field_evidence_version", "observed_requests",
                               "field_evidence_version IS NULL OR field_evidence_version = 1")
    op.create_table(
        "request_field_evidence",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("field_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["request_id", "field_id"],
                                ["request_field_usage.request_id", "request_field_usage.field_id"],
                                ondelete="CASCADE"),
        sa.CheckConstraint("state IN ('observed', 'not_observed', 'indeterminate', 'unavailable')",
                           name="valid_evidence_state"),
    )
    op.create_index("ix_field_evidence_field_state", "request_field_evidence", ["field_id", "state"])


def downgrade():
    op.drop_table("request_field_evidence")
    op.drop_constraint("valid_field_evidence_version", "observed_requests", type_="check")
    op.drop_column("observed_requests", "field_evidence_version")
