"""Shared contract with future proxy and GraphQL analyzer developers."""

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldReference:
    type_name: str
    field_name: str


@dataclass(frozen=True)
class GraphQLQueryInput:
    query: str
    operation_name: str | None = None
    variables: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisResult:
    status: str
    operation_type: str | None = None
    operation_name: str | None = None
    fields: tuple[FieldReference, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class RequestObservationInput:
    run_id: uuid.UUID
    analysis: AnalysisResult
    http_method: str
    http_status: int | None
    response_has_data: bool | None
    graphql_has_errors: bool | None
    duration_ms: int | None = None
    query_hash: str | None = None
