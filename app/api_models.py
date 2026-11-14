"""JSON contracts. Public API always serializes attributes as camelCase."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ServiceCreate(ApiModel):
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=255)


class ServiceOut(ApiModel):
    id: uuid.UUID
    key: str
    name: str
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
