"""Index history/journal and materialize the unchanged operation identity.

Revision ID: 20261117_history_journal
Revises: 20261116_service_endpoints
"""

import hashlib
import json

from alembic import op
import sqlalchemy as sa

revision = "20261117_history_journal"
down_revision = "20261116_service_endpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("observed_requests", sa.Column("operation_key", sa.String(64), nullable=True))
    # Keyset batches bound memory even for large historical journals. Python's
    # exact original JSON serialization also handles legacy Unicode identities.
    connection = op.get_bind()
    last = None
    while True:
        query = sa.text(
            'SELECT id, query_hash, operation_name, operation_type FROM observed_requests '
            + ('WHERE id > :last ' if last is not None else '')
            + 'ORDER BY id LIMIT 1000'
        )
        rows = connection.execute(query, {"last": last} if last is not None else {}).mappings().all()
        if not rows:
            break
        values = [{"request_id": row["id"], "key": hashlib.sha256(json.dumps(
            [row["query_hash"], row["operation_name"], row["operation_type"]],
            ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()} for row in rows
            if row["query_hash"] is not None and row["operation_type"] is not None]
        if values:
            connection.execute(sa.text('UPDATE observed_requests SET operation_key=:key WHERE id=:request_id'), values)
        last = rows[-1]["id"]
    op.create_index("ix_test_runs_snapshot_started_id", "test_runs", ["snapshot_id", "started_at", "id"])
    op.create_index("ix_requests_run_created_id", "observed_requests", ["run_id", "created_at", "id"])
    op.create_index("ix_requests_run_duration_id", "observed_requests", ["run_id", "duration_ms", "id"])
    op.create_index("ix_requests_run_operation_key", "observed_requests", ["run_id", "operation_key", "created_at", "id"],
                    postgresql_where=sa.text("operation_key IS NOT NULL"))


def downgrade():
    for name in ("ix_requests_run_operation_key", "ix_requests_run_duration_id", "ix_requests_run_created_id"):
        op.drop_index(name, table_name="observed_requests")
    op.drop_index("ix_test_runs_snapshot_started_id", table_name="test_runs")
    op.drop_column("observed_requests", "operation_key")
