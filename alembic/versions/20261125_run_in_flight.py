"""Track every admitted GraphQL exchange independently of test attribution."""
from alembic import op
import sqlalchemy as sa

revision = "20261125_run_in_flight"
down_revision = "20261124_test_executions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("test_runs", sa.Column("in_flight_requests", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("nonnegative_run_in_flight", "test_runs", "in_flight_requests >= 0")


def downgrade():
    op.drop_constraint("nonnegative_run_in_flight", "test_runs", type_="check")
    op.drop_column("test_runs", "in_flight_requests")
