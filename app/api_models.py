"""JSON contracts. Public API always serializes attributes as camelCase."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.contracts import OperationType
from app.graphql_errors import ErrorDetailsAvailability
from app.safe_document import DocumentAvailability


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ServiceCreate(ApiModel):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=255)
    is_public: bool = False
    endpoint_url: str = Field(min_length=1, max_length=2048)
    description: str | None = Field(default=None, max_length=2000)


class ServiceUpdate(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=2048)
    is_public: bool | None = None

    @model_validator(mode="after")
    def valid_update(self):
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set - {"description"}
        ):
            raise ValueError("Provide at least one change; only description may be null")
        return self


class ServiceOut(ApiModel):
    id: uuid.UUID
    key: str
    name: str
    is_public: bool
    created_at: datetime
    updated_at: datetime
    endpoint_url: str | None = None
    description: str | None = None


class SchemaImport(ApiModel):
    service_id: uuid.UUID
    sdl: str = Field(min_length=1, max_length=1_000_000)


class SchemaIntrospectionImport(ApiModel):
    # The upstream address comes only from the selected saved Service.
    model_config = ConfigDict(extra="forbid")
    service_id: uuid.UUID


class SchemaImportErrorOut(ApiModel):
    detail: str
    code: str = Field(description="Stable introspection error code; no upstream text, URL or credentials.")


class SchemaUpdate(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    display_name: str | None = Field(max_length=255)

    @model_validator(mode="after")
    def clean_label(self):
        if self.display_name is not None:
            self.display_name = self.display_name.strip()
            if not self.display_name:
                raise ValueError("displayName must not be blank")
        return self


class SchemaSdlOut(ApiModel):
    schema_id: uuid.UUID
    schema_hash: str
    sdl: str


class SchemaOut(ApiModel):
    display_name: str | None = None
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


class SchemaArgumentOut(ApiModel):
    name: str
    type: str
    description: str | None = None
    default_value: str | None = Field(default=None, description="Masked AST representation, never the actual schema argument default.")
    default_value_availability: Literal["absent", "available", "redacted", "unavailable"] = "absent"


class RootTypesOut(ApiModel):
    query: str | None = None
    mutation: str | None = None
    subscription: str | None = None


class GraphField(ApiModel):
    description: str | None = None
    deprecation_reason: str | None = None
    args: list[SchemaArgumentOut] = Field(default_factory=list)

    name: str
    return_type: str
    target_type: str | None
    deprecated: bool
    covered: bool
    hits: int


class GraphType(ApiModel):
    description: str | None = None
    root_operations: list[OperationType] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    possible_types: list[str] = Field(default_factory=list)

    name: str
    kind: str
    total_fields: int
    covered_fields: int
    coverage_percent: float | None
    fields: list[GraphField]


class GraphRelation(ApiModel):
    id: uuid.UUID = Field(description="Deterministic UUIDv5 of snapshot ID and [fromType, field, toType].")

    from_type: str
    field: str
    to_type: str
    covered: bool


class CoverageSummary(ApiModel):
    total_fields: int
    covered_fields: int
    coverage_percent: float


class CoverageGraph(ApiModel):
    root_types: RootTypesOut = Field(default_factory=RootTypesOut)

    schema_id: uuid.UUID
    run_id: uuid.UUID | None
    summary: CoverageSummary
    types: list[GraphType]
    relations: list[GraphRelation]


class RequestOut(ApiModel):
    operation_key: str | None = Field(default=None, description="Existing operation identity; null when hash or operation type is unknown.")
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


class SelectedFieldOut(ApiModel):
    type_name: str
    field_name: str
    return_type: str | None


class GraphQLErrorLocationOut(ApiModel):
    line: int = Field(ge=1, le=1_000_000)
    column: int = Field(ge=1, le=1_000_000)


class GraphQLErrorDetailOut(ApiModel):
    code: str | None = Field(default=None, max_length=64)
    path: list[str | int] | None = Field(default=None, max_length=16)
    locations: list[GraphQLErrorLocationOut] = Field(default_factory=list, max_length=8)
    safe_message: str | None = Field(default=None, max_length=128)


class RequestDetailOut(RequestOut):
    graphql_error_count: int | None = Field(default=None, ge=0, description="Actual number of upstream error objects, before truncation; null if unknown.")
    graphql_error_details_availability: ErrorDetailsAvailability = "unavailable"
    graphql_error_details: list[GraphQLErrorDetailOut] | None = Field(default=None, max_length=20)
    safe_graphql_document: str | None = Field(default=None, description="Normalized AST of the actual wire document, literals masked; never an executable replay or selectedFields reconstruction.")
    document_availability: DocumentAvailability = "unavailable"

    counted_in_coverage: bool
    selected_fields: list[SelectedFieldOut]


class OperationFieldOut(ApiModel):
    type_name: str
    field_name: str
    hits: int = Field(description="Counted requests selecting this field, once per request: successful analysis with response_has_data=true.")


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


class AnalysisStatusCounts(ApiModel):
    success: int
    parse_error: int
    validation_error: int
    unsupported: int
    internal_error: int


class RunStats(CoverageSummary):
    analysis_status_counts: AnalysisStatusCounts
    p95_duration_ms: float | None = Field(description="PostgreSQL percentile_cont(0.95) over this run's non-null durations; null for no measurements.")

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


class SchemaCoordinateOut(ApiModel):
    type_name: str
    field_name: str


class QueryPathOut(ApiModel):
    path: list[SchemaCoordinateOut]
    path_hits: int


class RelationHitOut(ApiModel):
    from_type: str
    field_name: str
    to_type: str
    relation_hits: int


class QueryPathPageOut(ApiModel):
    total: int
    items: list[QueryPathOut]


class RelationHitPageOut(ApiModel):
    total: int
    items: list[RelationHitOut]


class RunPathsOut(ApiModel):
    run_id: uuid.UUID
    snapshot_id: uuid.UUID
    path_data_availability: Literal["available", "partial", "unavailable"]
    max_depth: int | None
    paths: QueryPathPageOut
    relations: RelationHitPageOut


class ComparisonFieldPage(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    filter: Literal[
        "all", "changed", "newlyCovered", "noLongerCovered", "stillCovered",
        "stillUncovered", "alwaysCovered", "neverCovered", "intermittentlyCovered",
    ] = "all"
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class ComparisonTypePage(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class RunComparisonRequest(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    run_ids: list[uuid.UUID] = Field(min_length=2, max_length=20)
    baseline_run_id: uuid.UUID
    target_run_id: uuid.UUID | None = None
    fields: ComparisonFieldPage = Field(default_factory=ComparisonFieldPage)
    types: ComparisonTypePage = Field(default_factory=ComparisonTypePage)

    @model_validator(mode="after")
    def validate_run_selection(self):
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("runIds must not contain duplicates")
        selected = set(self.run_ids)
        if self.baseline_run_id not in selected:
            raise ValueError("baselineRunId must be included in runIds")
        if self.target_run_id is not None:
            if self.target_run_id not in selected:
                raise ValueError("targetRunId must be included in runIds")
            if self.target_run_id == self.baseline_run_id:
                raise ValueError("targetRunId must differ from baselineRunId")
        return self


class RunMetricOut(ApiModel):
    run_id: uuid.UUID
    name: str
    status: Literal["completed"]
    started_at: datetime
    finished_at: datetime | None
    total_fields: int
    covered_fields: int
    coverage_percent: float
    total_requests: int
    counted_requests: int
    unique_operations: int
    query_requests: int
    mutation_requests: int
    subscription_requests: int
    http_error_count: int
    graphql_error_count: int
    analysis_status_counts: AnalysisStatusCounts
    avg_duration_ms: float | None
    p95_duration_ms: float | None


class ComparisonDeltaOut(ApiModel):
    percentage_point_delta: float
    covered_fields_delta: int
    requests_delta: int
    counted_requests_delta: int
    unique_operations_delta: int
    http_error_count_delta: int
    graphql_error_count_delta: int
    avg_duration_delta_ms: float | None
    p95_duration_delta_ms: float | None
    adjacent_coverage_deltas_pp: list[dict[str, Any]]
    newly_covered: int
    no_longer_covered: int
    still_covered: int
    still_uncovered: int
    always_covered: int
    never_covered: int
    intermittently_covered: int


class RunFieldHitsOut(ApiModel):
    run_id: uuid.UUID
    hits: int


class ComparedFieldOut(ApiModel):
    type_name: str
    field_name: str
    hits_by_run: list[RunFieldHitsOut]
    covered_run_count: int
    coverage_frequency_percent: float
    coverage_transitions: int
    baseline_to_target: Literal["newlyCovered", "noLongerCovered", "stillCovered", "stillUncovered"]
    stability: Literal["alwaysCovered", "neverCovered", "intermittentlyCovered"]
    first_covered_run_id: uuid.UUID | None
    last_covered_run_id: uuid.UUID | None


class TypeRunCoverageOut(ApiModel):
    run_id: uuid.UUID
    covered_fields: int
    coverage_percent: float


class ComparedTypeOut(ApiModel):
    type_name: str
    root_role: Literal["query", "mutation", "subscription"] | None
    total_fields: int
    runs: list[TypeRunCoverageOut]


class ComparedFieldPageOut(ApiModel):
    total: int
    items: list[ComparedFieldOut]


class ComparedTypePageOut(ApiModel):
    total: int
    items: list[ComparedTypeOut]


class RunComparisonResponse(ApiModel):
    snapshot_id: uuid.UUID
    schema_hash: str
    generated_at: datetime
    baseline_run_id: uuid.UUID
    target_run_id: uuid.UUID
    runs: list[RunMetricOut]
    comparison: ComparisonDeltaOut
    field_categories: dict[str, int]
    fields: ComparedFieldPageOut
    types: ComparedTypePageOut
