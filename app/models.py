"""Persisted domain entities. GraphQL query/variable bodies are never stored."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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
    query_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_method: Mapped[str] = mapped_column(String(10), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(30), nullable=False)
    response_has_data: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    graphql_has_errors: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RequestFieldUsage(Base):
    __tablename__ = "request_field_usage"

    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("observed_requests.id", ondelete="CASCADE"), primary_key=True
    )
    field_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("schema_fields.id", ondelete="RESTRICT"), primary_key=True,
        index=True,
    )
