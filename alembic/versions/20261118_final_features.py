"""Add optional snapshot labels and bounded safe documents; no historic rewrite."""

from alembic import op
import sqlalchemy as sa

revision = "20261118_final_features"
down_revision = "20261117_history_journal"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("schema_snapshots", sa.Column("display_name", sa.String(255), nullable=True))
    op.add_column("observed_requests", sa.Column("safe_graphql_document", sa.Text(), nullable=True))
    op.add_column("observed_requests", sa.Column("document_availability", sa.String(16), nullable=False,
                                              server_default="unavailable"))
    op.create_check_constraint("valid_safe_document", "observed_requests",
        "(document_availability = 'unavailable' AND safe_graphql_document IS NULL) OR "
        "(document_availability IN ('available', 'redacted') AND safe_graphql_document IS NOT NULL "
        "AND octet_length(safe_graphql_document) BETWEEN 1 AND 65536)")


def downgrade():
    op.drop_constraint("valid_safe_document", "observed_requests", type_="check")
    op.drop_column("observed_requests", "document_availability")
    op.drop_column("observed_requests", "safe_graphql_document")
    op.drop_column("schema_snapshots", "display_name")
