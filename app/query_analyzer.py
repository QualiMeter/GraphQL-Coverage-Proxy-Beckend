"""Analyze GraphQL operations against a schema without persisting request data."""

from __future__ import annotations

from graphql import (
    GraphQLError,
    GraphQLIncludeDirective,
    GraphQLSchema,
    GraphQLSkipDirective,
    get_directive_values,
    get_named_type,
    get_variable_values,
    parse,
    validate,
)
from graphql.language import (
    FieldNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionSetNode,
)
from graphql.type import GraphQLInterfaceType, GraphQLObjectType

from app.contracts import AnalysisResult, FieldReference, GraphQLQueryInput


def analyze_query(schema: GraphQLSchema, request: GraphQLQueryInput) -> AnalysisResult:
    """Return unique schema coordinates selected by one valid query or mutation.

    The function is deliberately pure: it does not mutate the request, call an
    upstream server, or write to PostgreSQL. Unsupported constructs return no
    partial field list so they cannot inflate coverage with a guessed result.
    """
    try:
        return _analyze_query(schema, request)
    except Exception:
        # Analysis must never break transparent proxying. Do not expose request
        # text, variables, or exception details in the persisted error code.
        return AnalysisResult(status="internal_error", error_code="INTERNAL_ANALYZER_ERROR")


def _analyze_query(schema: GraphQLSchema, request: GraphQLQueryInput) -> AnalysisResult:
    if not isinstance(request.query, str) or not request.query.strip():
        return AnalysisResult(status="parse_error", error_code="EMPTY_QUERY")
    if not isinstance(request.variables, dict):
        return AnalysisResult(status="validation_error", error_code="INVALID_VARIABLES")

    try:
        document = parse(request.query)
    except GraphQLError:
        return AnalysisResult(status="parse_error", error_code="GRAPHQL_PARSE_ERROR")

    operations = [
        definition
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    if not operations:
        return AnalysisResult(status="validation_error", error_code="NO_OPERATION")

    if request.operation_name is None:
        if len(operations) != 1:
            return AnalysisResult(
                status="validation_error",
                error_code="OPERATION_NAME_REQUIRED",
            )
        operation = operations[0]
    else:
        operation = next(
            (
                candidate
                for candidate in operations
                if candidate.name is not None
                and candidate.name.value == request.operation_name
            ),
            None,
        )
        if operation is None:
            return AnalysisResult(
                status="validation_error",
                operation_name=request.operation_name,
                error_code="OPERATION_NOT_FOUND",
            )

    operation_type = operation.operation.value
    operation_name = operation.name.value if operation.name is not None else None
    if operation_type == "subscription":
        return AnalysisResult(
            status="unsupported",
            operation_type=operation_type,
            operation_name=operation_name,
            error_code="UNSUPPORTED_OPERATION_TYPE",
        )

    if _has_unsupported_selection(operation):
        return AnalysisResult(
            status="unsupported",
            operation_type=operation_type,
            operation_name=operation_name,
            error_code="UNSUPPORTED_SELECTION",
        )

    validation_errors = validate(schema, document)
    if validation_errors:
        return AnalysisResult(
            status="validation_error",
            operation_type=operation_type,
            operation_name=operation_name,
            error_code="GRAPHQL_VALIDATION_ERROR",
        )

    variable_values = get_variable_values(
        schema,
        operation.variable_definitions or (),
        request.variables,
    )
    if isinstance(variable_values, list):
        return AnalysisResult(
            status="validation_error",
            operation_type=operation_type,
            operation_name=operation_name,
            error_code="INVALID_VARIABLES",
        )

    root_type = (
        schema.query_type if operation_type == "query" else schema.mutation_type
    )
    if root_type is None:
        return AnalysisResult(
            status="validation_error",
            operation_type=operation_type,
            operation_name=operation_name,
            error_code="MISSING_ROOT_TYPE",
        )

    fields: list[FieldReference] = []
    seen: set[tuple[str, str]] = set()
    _collect_fields(root_type, operation.selection_set, variable_values, fields, seen)
    return AnalysisResult(
        status="success",
        operation_type=operation_type,
        operation_name=operation_name,
        fields=tuple(fields),
    )


def _has_unsupported_selection(operation: OperationDefinitionNode) -> bool:
    """Detect constructs whose field expansion is outside the MVP contract."""
    if _has_unsupported_directive(operation.directives or ()):
        return True

    pending = [operation.selection_set]
    while pending:
        selection_set = pending.pop()
        for selection in selection_set.selections:
            if isinstance(selection, (FragmentSpreadNode, InlineFragmentNode)):
                return True
            if _has_unsupported_directive(selection.directives or ()):
                return True
            if isinstance(selection, FieldNode) and selection.selection_set is not None:
                pending.append(selection.selection_set)
    return False


def _has_unsupported_directive(directives) -> bool:
    # @skip and @include are evaluated below; other execution directives could
    # change which fields actually execute and therefore must not be guessed.
    return any(
        directive.name.value not in {"skip", "include"}
        for directive in directives
    )


def _selection_is_included(selection: FieldNode, variables: dict) -> bool:
    skip = get_directive_values(GraphQLSkipDirective, selection, variables)
    if skip is not None and skip["if"]:
        return False
    include = get_directive_values(GraphQLIncludeDirective, selection, variables)
    return include is None or include["if"]


def _collect_fields(
    parent_type: GraphQLObjectType | GraphQLInterfaceType,
    selection_set: SelectionSetNode,
    variables: dict,
    fields: list[FieldReference],
    seen: set[tuple[str, str]],
) -> None:
    for selection in selection_set.selections:
        if not isinstance(selection, FieldNode) or not _selection_is_included(selection, variables):
            continue

        # Introspection and meta fields are not part of user schema coverage.
        field_name = selection.name.value
        if field_name.startswith("__"):
            continue

        coordinate = (parent_type.name, field_name)
        if coordinate not in seen:
            seen.add(coordinate)
            fields.append(FieldReference(*coordinate))

        field_definition = parent_type.fields[field_name]
        named_type = get_named_type(field_definition.type)
        if (
            selection.selection_set is not None
            and isinstance(named_type, (GraphQLObjectType, GraphQLInterfaceType))
        ):
            _collect_fields(named_type, selection.selection_set, variables, fields, seen)
