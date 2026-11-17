"""Persist incomplete collection and repair only provable historical deadlines."""
from alembic import op
import sqlalchemy as sa

revision = "20261123_incomplete_collection"
down_revision = "20261122_field_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gitlab_pipelines", sa.Column(
        "collection_incomplete", sa.Boolean(), nullable=False, server_default=sa.false()))
    # In the previous implementation only deadline could finish collection
    # while leaving its pipeline unfinished. A retained deadline error is also
    # direct evidence. Do not infer loss from elapsed time or terminal status:
    # old terminal polling could erase that evidence entirely.
    op.execute("""
        UPDATE gitlab_pipelines p SET collection_incomplete = true,
            error_code = 'tracking_deadline_exceeded'
        FROM test_runs r WHERE r.id = p.run_id AND (
            p.error_code = 'tracking_deadline_exceeded'
            OR (r.status = 'completed' AND p.finished_at IS NULL)
        )
    """)
    op.execute("""
        UPDATE test_runs r SET status = 'failed'
        FROM gitlab_pipelines p
        WHERE p.run_id = r.id AND p.collection_incomplete AND r.status = 'completed'
    """)


def downgrade():
    # Never restore an incomplete run to completed when rolling back the flag.
    op.drop_column("gitlab_pipelines", "collection_incomplete")
