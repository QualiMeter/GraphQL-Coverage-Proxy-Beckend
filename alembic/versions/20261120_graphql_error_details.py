"""Add bounded sanitized upstream errors; historical details stay unknown."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261120_graphql_error_details"
down_revision = "20261119_operation_name_text"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("observed_requests", sa.Column("graphql_error_count", sa.Integer(), nullable=True))
    op.add_column("observed_requests", sa.Column("graphql_error_details_availability", sa.String(16),
                                                nullable=False, server_default="unavailable"))
    op.add_column("observed_requests", sa.Column("graphql_error_details", postgresql.JSONB(none_as_null=True), nullable=True))
    op.create_check_constraint("valid_graphql_error_count", "observed_requests", 'graphql_error_count IS NULL OR graphql_error_count BETWEEN 0 AND 1048576')
    op.create_check_constraint("valid_graphql_error_details", "observed_requests", """
        (graphql_error_details_availability = 'unavailable' AND graphql_error_details IS NULL) OR
        (graphql_error_details_availability IN ('sanitized', 'truncated')
        AND graphql_error_count IS NOT NULL AND graphql_error_details IS NOT NULL
        AND CASE WHEN jsonb_typeof(graphql_error_details) = 'array' THEN
        jsonb_array_length(graphql_error_details) <= 20
        AND jsonb_array_length(graphql_error_details) <= graphql_error_count
        AND (graphql_error_details_availability = 'truncated' OR jsonb_array_length(graphql_error_details) = graphql_error_count)
        ELSE false END
        AND octet_length(graphql_error_details::text) <= 16384
        AND graphql_has_errors IS NOT NULL AND graphql_has_errors = (graphql_error_count > 0))
        """)


def downgrade():
    op.drop_constraint("valid_graphql_error_details", "observed_requests", type_="check")
    op.drop_constraint("valid_graphql_error_count", "observed_requests", type_="check")
    op.drop_column("observed_requests", "graphql_error_details")
    op.drop_column("observed_requests", "graphql_error_details_availability")
    op.drop_column("observed_requests", "graphql_error_count")
