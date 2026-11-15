"""Preserve full GraphQL operation names; refuse a lossy downgrade."""

from alembic import op
import sqlalchemy as sa

revision = "20261119_operation_name_text"
down_revision = "20261118_final_features"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("observed_requests", "operation_name",
                    existing_type=sa.String(255), type_=sa.Text(), existing_nullable=True)


def downgrade():
    # Hold the table lock through the check and ALTER so a concurrent insert
    # cannot introduce a long name between them. Never silently truncate names.
    op.execute("LOCK TABLE observed_requests IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM observed_requests WHERE char_length(operation_name) > 255) THEN
            RAISE EXCEPTION 'Cannot downgrade: operation names longer than 255 characters exist';
        END IF;
    END $$""")
    op.alter_column("observed_requests", "operation_name",
                    existing_type=sa.Text(), type_=sa.String(255), existing_nullable=True)
