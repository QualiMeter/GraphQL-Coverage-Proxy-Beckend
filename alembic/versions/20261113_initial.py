"""Create the seven core GraphQL coverage tables.

Revision ID: 20261113_initial
Revises:
Create Date: 2026-11-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261113_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graphql_services",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_graphql_services"),
        sa.UniqueConstraint("key", name="uq_graphql_services_key"),
    )

    op.create_table(
        "schema_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_sdl", sa.Text(), nullable=False),
        sa.Column("introspection_json", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source IN ('sdl', 'introspection')", name="ck_schema_snapshots_valid_source"),
        sa.ForeignKeyConstraint(["service_id"], ["graphql_services.id"], name="fk_schema_snapshots_service_id_graphql_services", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_schema_snapshots"),
        sa.UniqueConstraint("service_id", "schema_hash", name="uq_schema_snapshots_service_hash"),
    )
    op.create_index("ix_schema_snapshots_service_id", "schema_snapshots", ["service_id"])

    op.create_table(
        "schema_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["schema_snapshots.id"], name="fk_schema_types_snapshot_id_schema_snapshots", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_schema_types"),
        sa.UniqueConstraint("snapshot_id", "name", name="uq_schema_types_snapshot_name"),
    )
    op.create_index("ix_schema_types_snapshot_id", "schema_types", ["snapshot_id"])

    op.create_table(
        "schema_fields",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("return_type", sa.Text(), nullable=False),
        sa.Column("target_type_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_deprecated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(["type_id"], ["schema_types.id"], name="fk_schema_fields_type_id_schema_types", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_type_id"], ["schema_types.id"], name="fk_schema_fields_target_type_id_schema_types", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_schema_fields"),
        sa.UniqueConstraint("type_id", "name", name="uq_schema_fields_type_name"),
    )
    op.create_index("ix_schema_fields_type_id", "schema_fields", ["type_id"])
    op.create_index("ix_schema_fields_target_type_id", "schema_fields", ["target_type_id"])

    op.create_table(
        "test_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_test_runs_valid_status"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["schema_snapshots.id"], name="fk_test_runs_snapshot_id_schema_snapshots", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_test_runs"),
    )
    op.create_index("ix_test_runs_snapshot_id", "test_runs", ["snapshot_id"])
    op.create_index("uq_test_runs_running_snapshot", "test_runs", ["snapshot_id"], unique=True, postgresql_where=sa.text("status = 'running'"))

    op.create_table(
        "observed_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_name", sa.String(length=255), nullable=True),
        sa.Column("operation_type", sa.String(length=20), nullable=True),
        sa.Column("query_hash", sa.String(length=64), nullable=True),
        sa.Column("http_method", sa.String(length=10), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("analysis_status", sa.String(length=30), nullable=False),
        sa.Column("response_has_data", sa.Boolean(), nullable=True),
        sa.Column("graphql_has_errors", sa.Boolean(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("operation_type IS NULL OR operation_type IN ('query', 'mutation', 'subscription')", name="ck_observed_requests_valid_operation_type"),
        sa.CheckConstraint("analysis_status IN ('success', 'parse_error', 'validation_error', 'unsupported', 'internal_error')", name="ck_observed_requests_valid_analysis_status"),
        sa.CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="ck_observed_requests_valid_http_status"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="ck_observed_requests_nonnegative_duration"),
        sa.ForeignKeyConstraint(["run_id"], ["test_runs.id"], name="fk_observed_requests_run_id_test_runs", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_observed_requests"),
    )
    op.create_index("ix_observed_requests_run_id", "observed_requests", ["run_id"])

    op.create_table(
        "request_field_usage",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["observed_requests.id"], name="fk_request_field_usage_request_id_observed_requests", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["field_id"], ["schema_fields.id"], name="fk_request_field_usage_field_id_schema_fields", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("request_id", "field_id", name="pk_request_field_usage"),
    )
    op.create_index("ix_request_field_usage_field_id", "request_field_usage", ["field_id"])


def downgrade() -> None:
    op.drop_index("ix_request_field_usage_field_id", table_name="request_field_usage")
    op.drop_table("request_field_usage")
    op.drop_index("ix_observed_requests_run_id", table_name="observed_requests")
    op.drop_table("observed_requests")
    op.drop_index("uq_test_runs_running_snapshot", table_name="test_runs")
    op.drop_index("ix_test_runs_snapshot_id", table_name="test_runs")
    op.drop_table("test_runs")
    op.drop_index("ix_schema_fields_target_type_id", table_name="schema_fields")
    op.drop_index("ix_schema_fields_type_id", table_name="schema_fields")
    op.drop_table("schema_fields")
    op.drop_index("ix_schema_types_snapshot_id", table_name="schema_types")
    op.drop_table("schema_types")
    op.drop_index("ix_schema_snapshots_service_id", table_name="schema_snapshots")
    op.drop_table("schema_snapshots")
    op.drop_table("graphql_services")
