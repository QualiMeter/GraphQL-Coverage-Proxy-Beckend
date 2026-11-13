"""Shared contract with future proxy and GraphQL analyzer developers."""

import uuid
from dataclasses import dataclass, field
from typing import Literal


AnalysisStatus = Literal[
    "success", "parse_error", "validation_error", "unsupported", "internal_error"
]
OperationType = Literal["query", "mutation", "subscription"]


@dataclass(frozen=True)
class FieldReference:
    """A schema coordinate; aliases are resolved to the underlying field name."""

    type_name: str
    field_name: str


@dataclass(frozen=True)
class GraphQLQueryInput:
    query: str
    operation_name: str | None = None
    variables: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisResult:
    """Shared analyzer result consumed by coverage storage.

    A successful result contains unique ``(type_name, field_name)`` schema
    coordinates for the selected fields. Failed or unsupported analyses must
    use an empty ``fields`` tuple.
    """

    status: AnalysisStatus
    operation_type: OperationType | None = None
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
