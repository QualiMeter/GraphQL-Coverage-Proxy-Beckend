"""Persist GitLab launch intents and restartable polling leases; no tokens."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261121_gitlab_pipelines"
down_revision = "20261120_graphql_error_details"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gitlab_pipelines",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("test_runs.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("gitlab_host", sa.String(2048), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("pipeline_id", sa.BigInteger(), nullable=True),
        sa.Column("pipeline_url", sa.String(2048), nullable=True),
        sa.Column("pipeline_status", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_page", sa.Integer(), nullable=False),
        sa.Column("check_failures", sa.Integer(), nullable=False),
        sa.UniqueConstraint("gitlab_host", "project_id", "pipeline_id", name="uq_gitlab_pipeline_external"),
    )
    op.create_index("ix_gitlab_pipelines_next_check_at", "gitlab_pipelines", ["next_check_at"])


def downgrade():
    op.drop_table("gitlab_pipelines")
