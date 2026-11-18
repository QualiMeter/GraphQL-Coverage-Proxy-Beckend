from alembic import op
import sqlalchemy as sa

revision = "20261127_test_groups"
down_revision = "20261126_argument_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("test_executions", sa.Column("team_key", sa.String(128), nullable=True))
    op.add_column("test_executions", sa.Column("suite_key", sa.String(128), nullable=True))
    op.create_index("ix_test_executions_run_team", "test_executions", ["run_id", "team_key"])
    op.create_index("ix_test_executions_run_suite", "test_executions", ["run_id", "suite_key"])


def downgrade():
    op.drop_index("ix_test_executions_run_suite", table_name="test_executions")
    op.drop_index("ix_test_executions_run_team", table_name="test_executions")
    op.drop_column("test_executions", "suite_key")
    op.drop_column("test_executions", "team_key")
