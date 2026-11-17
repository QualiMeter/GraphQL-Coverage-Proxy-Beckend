"""Persist normalized canonical query paths and request path hits."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261122_query_paths"
down_revision = "20261122_field_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("observed_requests", sa.Column("max_depth", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "valid_analysis_max_depth", "observed_requests",
        "max_depth IS NULL OR max_depth BETWEEN 0 AND 64",
    )
    op.create_table(
        "query_paths",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("schema_snapshots.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("path_hash", sa.String(64), nullable=False),
        sa.Column("max_depth", sa.Integer(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "path_hash", name="uq_query_paths_snapshot_hash"),
        sa.CheckConstraint("max_depth BETWEEN 1 AND 64", name="valid_query_path_depth"),
    )
    op.create_index("ix_query_paths_snapshot_id", "query_paths", ["snapshot_id"])
    op.create_table(
        "query_path_fields",
        sa.Column("path_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("query_paths.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("field_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("schema_fields.id", ondelete="RESTRICT"), nullable=False),
        sa.CheckConstraint("position >= 0", name="nonnegative_path_position"),
    )
    op.create_index("ix_query_path_fields_field_id", "query_path_fields", ["field_id"])
    op.create_table(
        "request_path_usage",
        sa.Column("request_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("observed_requests.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("path_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("query_paths.id", ondelete="RESTRICT"), primary_key=True),
    )
    op.create_index("ix_request_path_usage_path_id", "request_path_usage", ["path_id"])


def downgrade():
    op.drop_index("ix_request_path_usage_path_id", table_name="request_path_usage")
    op.drop_table("request_path_usage")
    op.drop_index("ix_query_path_fields_field_id", table_name="query_path_fields")
    op.drop_table("query_path_fields")
    op.drop_index("ix_query_paths_snapshot_id", table_name="query_paths")
    op.drop_table("query_paths")
    op.drop_constraint("valid_analysis_max_depth", "observed_requests", type_="check")
    op.drop_column("observed_requests", "max_depth")
