"""Persisted domain entities. GraphQL query/variable bodies are never stored."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    event,
    Boolean,
    CheckConstraint,
    ForeignKey,
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


class ObservedRequest(Base):
    __tablename__ = "observed_requests"
    __table_args__ = (
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
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    operation_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operation_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    operation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    query_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_method: Mapped[str] = mapped_column(String(10), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(30), nullable=False)
    response_has_data: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    graphql_has_errors: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


# Register additive auth models for Alembic metadata and application imports.
from app.auth_models import BrowserSession, LoginThrottle, ServiceGrant, User  # noqa: E402, F401
