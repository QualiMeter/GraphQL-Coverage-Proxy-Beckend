"""JSON contracts. Public API always serializes attributes as camelCase."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.contracts import OperationType


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ServiceCreate(ApiModel):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=255)
    is_public: bool = False


class ServiceVisibilityUpdate(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    is_public: bool


class ServiceOut(ApiModel):
    id: uuid.UUID
    key: str
    name: str
    is_public: bool
    created_at: datetime


class SchemaImport(ApiModel):
    service_id: uuid.UUID
    sdl: str = Field(min_length=1, max_length=1_000_000)


class SchemaIntrospectionImport(ApiModel):
    # The upstream address must come from server configuration only.
    model_config = ConfigDict(extra="forbid")
    service_id: uuid.UUID


class SchemaOut(ApiModel):
    id: uuid.UUID
    service_id: uuid.UUID
    schema_hash: str
    source: str
    created_at: datetime


class RunCreate(ApiModel):
    snapshot_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunOut(ApiModel):
    id: uuid.UUID
    snapshot_id: uuid.UUID
    name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    metadata: dict[str, Any]


class GraphField(ApiModel):
    name: str
    return_type: str
    target_type: str | None
    deprecated: bool
    covered: bool
    hits: int


class GraphType(ApiModel):
    name: str
    kind: str
    total_fields: int
    covered_fields: int
    coverage_percent: float | None
    fields: list[GraphField]


class GraphRelation(ApiModel):
    from_type: str
    field: str
    to_type: str
    covered: bool


class CoverageSummary(ApiModel):
    total_fields: int
    covered_fields: int
    coverage_percent: float


class CoverageGraph(ApiModel):
    schema_id: uuid.UUID
    run_id: uuid.UUID | None
    summary: CoverageSummary
    types: list[GraphType]
    relations: list[GraphRelation]


class RequestOut(ApiModel):
    id: uuid.UUID
    run_id: uuid.UUID
    operation_name: str | None
    operation_type: str | None
    http_method: str
    http_status: int | None
    analysis_status: str
    response_has_data: bool | None
    graphql_has_errors: bool | None
    duration_ms: int | None
    error_code: str | None
    created_at: datetime


class OperationFieldOut(ApiModel):
    type_name: str
    field_name: str
    request_count: int = Field(description="Requests selecting this field, once per request, including uncounted selections.")


class OperationOut(ApiModel):
    operation_key: str = Field(description="SHA-256 of the JSON tuple [query_hash, operationName, operationType]. Stable across runs.")
    operation_name: str | None
    operation_type: OperationType
    request_count: int
    counted_request_count: int = Field(description="Successful analysis with response_has_data=true, as in Coverage Engine.")
    graphql_error_count: int
    http_error_count: int
    avg_duration_ms: float | None = Field(description="Mean over known durations only; null when none are known.")
    last_seen_at: datetime
    fields: list[OperationFieldOut]


class RequestTimelineBucket(ApiModel):
    bucket_start: datetime = Field(description="Inclusive start of a one-minute UTC interval; end is exclusive. Empty intervals are omitted.")
    request_count: int
    counted_request_count: int
    graphql_error_count: int
    http_error_count: int
    avg_duration_ms: float | None


class RunStats(CoverageSummary):
    total_requests: int
    counted_requests: int
    unique_operations: int = Field(description="Groups with known query_hash and operation_type, including anonymous operations.")
    query_requests: int
    mutation_requests: int
    subscription_requests: int = Field(description="Observed subscription metadata only; subscriptions remain unsupported by the analyzer.")
    http_error_count: int
    graphql_error_count: int
    avg_duration_ms: float | None = Field(description="Mean over known durations only; null when none are known.")
    request_timeline: list[RequestTimelineBucket]
