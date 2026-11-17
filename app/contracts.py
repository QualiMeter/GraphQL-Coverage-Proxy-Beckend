"""Shared types for GraphQL analysis and observation storage."""

import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.safe_document import SafeDocument
from app.graphql_errors import ErrorSummary


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
    """Unique selected schema coordinates; unsuccessful analyses have no fields."""

    status: AnalysisStatus
    operation_type: OperationType | None = None
    operation_name: str | None = None
    fields: tuple[FieldReference, ...] = ()
    paths: tuple[tuple[FieldReference, ...], ...] = ()
    max_depth: int | None = None
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
    safe_document: SafeDocument = field(default_factory=SafeDocument)
    graphql_errors: ErrorSummary = field(default_factory=ErrorSummary)
