"""Nullable service endpoints; preserve all historic data without guessed URLs."""
from alembic import op
import sqlalchemy as sa

revision = "20261116_service_endpoints"
down_revision = "20261115_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("graphql_services", sa.Column("endpoint_url", sa.String(2048), nullable=True))
    op.add_column("graphql_services", sa.Column("description", sa.String(2000), nullable=True))
    op.add_column("graphql_services", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE graphql_services SET updated_at = created_at")
    op.alter_column("graphql_services", "updated_at", nullable=False, server_default=sa.func.now())


def downgrade():
    op.drop_column("graphql_services", "updated_at")
    op.drop_column("graphql_services", "description")
    op.drop_column("graphql_services", "endpoint_url")
