"""Shared types for GraphQL analysis and observation storage."""

import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.safe_document import SafeDocument
from app.graphql_errors import ErrorSummary


AnalysisStatus = Literal[
    "success", "parse_error", "validation_error", "unsupported", "internal_error"
]
RunStatus = Literal["running", "completed", "failed"]
OperationType = Literal["query", "mutation", "subscription"]
EvidenceState = Literal["observed", "not_observed", "indeterminate", "unavailable"]


@dataclass(frozen=True)
class FieldReference:
    """A schema coordinate; aliases are resolved to the underlying field name."""

    type_name: str
    field_name: str


@dataclass(frozen=True)
class SelectionScope:
    """In-memory response object plan, shared with the fragments traversal.

    possible_types are concrete schema object names. Fields retain their static
    schema coordinate; applicable_types is the intersection of type conditions
    at this object level. Never persist this plan (aliases are client input).
    """

    possible_types: frozenset[str]
    fields: tuple["PlannedField", ...] = ()


@dataclass(frozen=True)
class PlannedField:
    coordinate: FieldReference | None  # None only for __typename
    response_key: str
    applicable_types: frozenset[str]
    child: SelectionScope | None = None
    # Number of GraphQL list wrappers; non-null wrappers are irrelevant to
    # presence. Zero means an object, one a list of objects.
    list_depth: int = 0


@dataclass(frozen=True)
class FieldEvidence:
    field: FieldReference
    state: EvidenceState


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
    error_code: str | None = None
    selection_plan: SelectionScope | None = None


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
    field_evidence: tuple[FieldEvidence, ...] | None = None
