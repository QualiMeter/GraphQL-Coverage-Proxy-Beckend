"""Store CI metadata and schedule backfill for known pipelines."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261125_gitlab_history"
down_revision = "20261124_test_executions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gitlab_pipelines", sa.Column("commit_sha", sa.String(64)))
    op.add_column("gitlab_pipelines", sa.Column("environment", sa.String(128)))
    op.add_column("gitlab_pipelines", sa.Column("jobs_next_check_at", sa.DateTime(timezone=True)))
    op.add_column("gitlab_pipelines", sa.Column("jobs_synced_at", sa.DateTime(timezone=True)))
    op.add_column("gitlab_pipelines", sa.Column("jobs_page", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("gitlab_pipelines", sa.Column("jobs_lease_token", postgresql.UUID(as_uuid=True)))
    for column in ("pipeline_id", "commit_sha", "environment", "jobs_next_check_at"):
        op.create_index(f"ix_gitlab_pipelines_{column}", "gitlab_pipelines", [column])
    op.create_index("ix_gitlab_pipelines_confirmed_ref", "gitlab_pipelines", ["ref", "run_id"],
                    postgresql_where=sa.text("pipeline_id IS NOT NULL"))
    op.create_table(
        "gitlab_jobs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("gitlab_pipelines.run_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("job_id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("stage", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_gitlab_jobs_job_id", "gitlab_jobs", ["job_id"])
    op.create_index("ix_gitlab_jobs_name", "gitlab_jobs", ["name"])
    op.execute("UPDATE gitlab_pipelines SET jobs_next_check_at = now() WHERE pipeline_id IS NOT NULL")


def downgrade():
    op.drop_table("gitlab_jobs")
    op.drop_index("ix_gitlab_pipelines_confirmed_ref", table_name="gitlab_pipelines")
    for column in ("pipeline_id", "commit_sha", "environment", "jobs_next_check_at"):
        op.drop_index(f"ix_gitlab_pipelines_{column}", table_name="gitlab_pipelines")
    for column in ("jobs_lease_token", "jobs_page", "jobs_synced_at", "jobs_next_check_at", "environment", "commit_sha"):
        op.drop_column("gitlab_pipelines", column)
