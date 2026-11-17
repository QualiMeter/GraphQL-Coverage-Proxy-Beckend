from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261124_test_executions"
down_revision = "20261123_incomplete_collection"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "test_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("test_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("test_id", sa.String(2048), nullable=False),
        sa.Column("test_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("framework", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.BigInteger()),
        sa.Column("failure_code", sa.String(32)),
        sa.Column("in_flight_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("id", "run_id", name="uq_test_executions_id_run"),
        sa.UniqueConstraint("run_id", "framework", "test_key", "attempt", name="uq_test_executions_attempt"),
        sa.CheckConstraint("framework IN ('pytest', 'playwright')", name="valid_test_framework"),
        sa.CheckConstraint("attempt BETWEEN 1 AND 10000", name="valid_test_attempt"),
        sa.CheckConstraint("in_flight_requests >= 0", name="nonnegative_test_in_flight"),
        sa.CheckConstraint("status IN ('running', 'passed', 'failed', 'skipped', 'interrupted')", name="valid_test_status"),
        sa.CheckConstraint("(status = 'running' AND finished_at IS NULL AND duration_ms IS NULL) OR "
                           "(status <> 'running' AND finished_at IS NOT NULL AND duration_ms IS NOT NULL AND duration_ms >= 0)",
                           name="valid_test_completion"),
        sa.CheckConstraint("failure_code IS NULL OR failure_code IN ('test_failed', 'assertion_failed', 'setup_failed', "
                           "'teardown_failed', 'timeout', 'runner_interrupted')", name="valid_test_failure_code"),
    )
    op.create_index("ix_test_executions_run_started_id", "test_executions", ["run_id", "started_at", "id"])
    op.add_column("observed_requests", sa.Column("test_execution_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_observed_requests_test_execution_run", "observed_requests", "test_executions",
                          ["test_execution_id", "run_id"], ["id", "run_id"], ondelete="RESTRICT")
    op.create_index("ix_requests_test_execution_created_id", "observed_requests", ["test_execution_id", "created_at", "id"])


def downgrade():
    op.drop_index("ix_requests_test_execution_created_id", table_name="observed_requests")
    op.drop_constraint("fk_observed_requests_test_execution_run", "observed_requests", type_="foreignkey")
    op.drop_column("observed_requests", "test_execution_id")
    op.drop_index("ix_test_executions_run_started_id", table_name="test_executions")
    op.drop_table("test_executions")
