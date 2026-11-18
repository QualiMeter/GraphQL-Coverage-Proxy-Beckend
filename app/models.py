"""Persisted domain entities. Only masked query AST copies are stored; raw queries/variables never are."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    event,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy.types import DateTime

from app.database import Base


class GraphQLService(Base):
    __tablename__ = "graphql_services"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    endpoint_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SchemaSnapshot(Base):
    __tablename__ = "schema_snapshots"
    __table_args__ = (
        UniqueConstraint("service_id", "schema_hash", name="uq_schema_snapshots_service_hash"),
        CheckConstraint("source IN ('sdl', 'introspection')", name="valid_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("graphql_services.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_sdl: Mapped[str] = mapped_column(Text, nullable=False)
    introspection_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SchemaType(Base):
    __tablename__ = "schema_types"
    __table_args__ = (UniqueConstraint("snapshot_id", "name", name="uq_schema_types_snapshot_name"),)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_snapshots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)


class SchemaField(Base):
    __tablename__ = "schema_fields"
    __table_args__ = (UniqueConstraint("type_id", "name", name="uq_schema_fields_type_name"),)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_types.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    return_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_types.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    is_deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class TestRun(Base):
    __tablename__ = "test_runs"
    __table_args__ = (
        Index("ix_test_runs_snapshot_started_id", "snapshot_id", "started_at", "id"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="valid_status"),
        Index(
            "uq_test_runs_running_snapshot", "snapshot_id", unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_snapshots.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class TestExecution(Base):
    __tablename__ = "test_executions"
    __table_args__ = (
        UniqueConstraint("id", "run_id", name="uq_test_executions_id_run"),
        UniqueConstraint("run_id", "framework", "test_key", "attempt", name="uq_test_executions_attempt"),
        CheckConstraint("framework IN ('pytest', 'playwright')", name="valid_test_framework"),
        CheckConstraint("attempt BETWEEN 1 AND 10000", name="valid_test_attempt"),
        CheckConstraint("in_flight_requests >= 0", name="nonnegative_test_in_flight"),
        CheckConstraint("status IN ('running', 'passed', 'failed', 'skipped', 'interrupted')", name="valid_test_status"),
        CheckConstraint("(status = 'running' AND finished_at IS NULL AND duration_ms IS NULL) OR "
                        "(status <> 'running' AND finished_at IS NOT NULL AND duration_ms IS NOT NULL AND duration_ms >= 0)",
                        name="valid_test_completion"),
        CheckConstraint("failure_code IS NULL OR failure_code IN ('test_failed', 'assertion_failed', 'setup_failed', "
                        "'teardown_failed', 'timeout', 'runner_interrupted')", name="valid_test_failure_code"),
        Index("ix_test_executions_run_started_id", "run_id", "started_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="RESTRICT"), nullable=False)
    test_id: Mapped[str] = mapped_column(String(2048), nullable=False)
    test_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    framework: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    in_flight_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ObservedRequest(Base):
    __tablename__ = "observed_requests"
    __table_args__ = (
        ForeignKeyConstraint(["test_execution_id", "run_id"], ["test_executions.id", "test_executions.run_id"],
                             name="fk_observed_requests_test_execution_run", ondelete="RESTRICT"),
        Index("ix_requests_test_execution_created_id", "test_execution_id", "created_at", "id"),
        CheckConstraint(
            "(document_availability = 'unavailable' AND safe_graphql_document IS NULL) OR "
            "(document_availability IN ('available', 'redacted') AND safe_graphql_document IS NOT NULL "
            "AND octet_length(safe_graphql_document) BETWEEN 1 AND 65536)",
            name="valid_safe_document",
        ),
        CheckConstraint(
            "graphql_error_count IS NULL OR graphql_error_count BETWEEN 0 AND 1048576",
            name="valid_graphql_error_count",
        ),
        CheckConstraint("""
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
            """, name="valid_graphql_error_details"),
        CheckConstraint("field_evidence_version IS NULL OR field_evidence_version = 1", name="valid_field_evidence_version"),
        Index("ix_requests_run_created_id", "run_id", "created_at", "id"),
        Index("ix_requests_run_duration_id", "run_id", "duration_ms", "id"),
        Index("ix_requests_run_operation_key", "run_id", "operation_key", "created_at", "id",
              postgresql_where=text("operation_key IS NOT NULL")),
        CheckConstraint(
            "operation_type IS NULL OR operation_type IN ('query', 'mutation', 'subscription')",
            name="valid_operation_type",
        ),
        CheckConstraint(
            "analysis_status IN ('success', 'parse_error', 'validation_error', 'unsupported', 'internal_error')",
            name="valid_analysis_status",
        ),
        CheckConstraint("http_status IS NULL OR http_status BETWEEN 100 AND 599", name="valid_http_status"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="nonnegative_duration"),
        CheckConstraint("max_depth IS NULL OR max_depth BETWEEN 0 AND 64", name="valid_analysis_max_depth"),
    )

    field_evidence_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    test_execution_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    operation_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_graphql_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_availability: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unavailable")
    query_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_method: Mapped[str] = mapped_column(String(10), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(30), nullable=False)
    response_has_data: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    graphql_has_errors: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    graphql_error_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graphql_error_details_availability: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="unavailable")
    graphql_error_details: Mapped[list[dict] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


@event.listens_for(ObservedRequest, "before_insert")
@event.listens_for(ObservedRequest, "before_update")
def update_operation_key(mapper, connection, row):
    # All application observation writes use the ORM. Keep the indexed identity
    # in sync even when historical metadata is edited through that API.
    from app.operation_identity import operation_key
    row.operation_key = (
        operation_key(row.query_hash, row.operation_name, row.operation_type)
        if row.query_hash is not None and row.operation_type is not None else None
    )


class RequestFieldUsage(Base):
    __tablename__ = "request_field_usage"

    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("observed_requests.id", ondelete="CASCADE"), primary_key=True
    )
    field_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_fields.id", ondelete="RESTRICT"), primary_key=True,
        index=True,
    )


class QueryPath(Base):
    """Snapshot-specific canonical path of schema field coordinates."""
    __tablename__ = "query_paths"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "path_hash", name="uq_query_paths_snapshot_hash"),
        CheckConstraint("max_depth BETWEEN 1 AND 64", name="valid_query_path_depth"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_snapshots.id", ondelete="RESTRICT"), nullable=False, index=True,
    )
    path_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False)


class QueryPathField(Base):
    """Ordered schema-field coordinates belonging to a canonical path."""
    __tablename__ = "query_path_fields"
    __table_args__ = (CheckConstraint("position >= 0", name="nonnegative_path_position"),)

    path_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("query_paths.id", ondelete="CASCADE"), primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_fields.id", ondelete="RESTRICT"), nullable=False, index=True,
    )


class RequestPathUsage(Base):
    """A request contributes at most one hit to each canonical analyzed path."""
    __tablename__ = "request_path_usage"

    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("observed_requests.id", ondelete="CASCADE"), primary_key=True,
    )
    path_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("query_paths.id", ondelete="RESTRICT"), primary_key=True, index=True,
    )


class RequestFieldEvidence(Base):
    __tablename__ = "request_field_evidence"
    __table_args__ = (
        ForeignKeyConstraint(["request_id", "field_id"],
                             ["request_field_usage.request_id", "request_field_usage.field_id"],
                             ondelete="CASCADE"),
        CheckConstraint("state IN ('observed', 'not_observed', 'indeterminate', 'unavailable')",
                        name="valid_evidence_state"),
        Index("ix_field_evidence_field_state", "field_id", "state"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    field_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)


# Register additive auth models for Alembic metadata and application imports.
from app.auth_models import BrowserSession, LoginThrottle, ServiceGrant, User  # noqa: E402, F401


class GitLabPipeline(Base):
    __tablename__ = "gitlab_pipelines"
    __table_args__ = (UniqueConstraint("gitlab_host", "project_id", "pipeline_id",
                                      name="uq_gitlab_pipeline_external"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="RESTRICT"), primary_key=True,
    )
    gitlab_host: Mapped[str] = mapped_column(String(2048), nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ref: Mapped[str] = mapped_column(String(255), nullable=False)
    pipeline_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pipeline_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    pipeline_status: Mapped[str] = mapped_column(String(64), nullable=False, default="creating")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collection_incomplete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconcile_page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    check_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
