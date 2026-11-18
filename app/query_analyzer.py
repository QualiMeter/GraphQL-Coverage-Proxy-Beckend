"""Analyze GraphQL operations against a schema without persisting request data."""

from __future__ import annotations

from graphql import (
    GraphQLError,
    GraphQLCompositeType,
    GraphQLIncludeDirective,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLSkipDirective,
    get_directive_values,
    get_named_type,
    get_variable_values,
    is_composite_type,
    parse,
    validate,
)
from graphql.language import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    OperationDefinitionNode,
    SelectionNode,
    SelectionSetNode,
    NullValueNode,
    VariableNode,
)

from app.contracts import ArgumentUsage, AnalysisResult, FieldReference, GraphQLQueryInput, PlannedField, SelectionScope
from app.diagnostics import internal_failure


MAX_QUERY_TOKENS = 10_000
MAX_SELECTION_DEPTH = 64
MAX_SELECTION_VISITS = 20_000
MAX_PATHS_PER_REQUEST = 5_000
MAX_ARGUMENTS_PER_REQUEST = 5_000
MAX_ARGUMENT_VISITS = 20_000


class _UnsupportedDirective(Exception):
    pass


class _AnalysisLimitExceeded(Exception):
    pass


def analyze_query(schema: GraphQLSchema, request: GraphQLQueryInput) -> AnalysisResult:
    """Return unique schema coordinates selected by one valid query or mutation.

    The function is deliberately pure: it does not mutate the request, call an
    upstream server, or write to PostgreSQL. Unsupported constructs return no
    partial field list so they cannot inflate coverage with a guessed result.
    """
    try:
        return _analyze_query(schema, request)
    except RecursionError:
        # Parsing or validating a deeply nested document can hit Python's limit.
        return AnalysisResult(status="unsupported", error_code="ANALYSIS_LIMIT_EXCEEDED")
    except Exception as exc:
        internal_failure("analyzer", exc)
        # Analysis must never break transparent proxying. Do not expose request
        # text, variables, or exception details in the persisted error code.
        return AnalysisResult(status="internal_error", error_code="INTERNAL_ANALYZER_ERROR")


def _analyze_query(schema: GraphQLSchema, request: GraphQLQueryInput) -> AnalysisResult:
    if not isinstance(request.query, str) or not request.query.strip():
        return AnalysisResult(status="parse_error", error_code="EMPTY_QUERY")
    if request.variables is not None and not isinstance(request.variables, dict):
        return AnalysisResult(status="validation_error", error_code="INVALID_VARIABLES")

    try:
        document = parse(request.query, max_tokens=MAX_QUERY_TOKENS)
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
                error_code="OPERATION_NOT_FOUND",
            )

    operation_type = operation.operation.value
    operation_name = operation.name.value if operation.name is not None else None
    if operation_type == "subscription" or _has_unsupported_directive(operation):
        return AnalysisResult(
            status="unsupported",
            operation_type=operation_type,
            operation_name=operation_name,
            error_code="UNSUPPORTED_OPERATION_TYPE" if operation_type == "subscription" else "UNSUPPORTED_SELECTION",
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
        request.variables or {},
    )
    invalid_variables = isinstance(variable_values, list) or request.variables is None
    # A coercion error never becomes selected-field coverage. Argument syntax
    # can still be classified, conservatively treating every variable as unknown.
    # graphql-core 3.2 returns a dict; 3.3 returns VariableValues with
    # .coerced and .sources. Directives need the original version-native
    # object, while argument nullness reads only the coerced mapping.
    argument_variables = None if invalid_variables else getattr(variable_values, "coerced", variable_values)
    if invalid_variables:
        # Both versions accept None for unavailable variables. A bare dict
        # would break 3.3 directive coercion instead of yielding GraphQLError.
        variable_values = None

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

    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    try:
        for variable in operation.variable_definitions or ():
            _check_directives(variable)
        fields, plan, paths, max_depth, arguments = _collect_fields(
            schema, root_type, operation.selection_set, fragments, variable_values, argument_variables,
        )
    except GraphQLError:
        # Runtime directive conditions could not be resolved. Do not guess which
        # fields were selected, or record omitted/provided for skipped branches.
        return AnalysisResult(status="validation_error", operation_type=operation_type,
                              operation_name=operation_name, error_code="INVALID_VARIABLES")
    except (_UnsupportedDirective, _AnalysisLimitExceeded) as exc:
        return AnalysisResult(
            status="unsupported",
            operation_type=operation_type,
            operation_name=operation_name,
            error_code="UNSUPPORTED_SELECTION" if isinstance(exc, _UnsupportedDirective) else "ANALYSIS_LIMIT_EXCEEDED",
        )
    if invalid_variables:
        return AnalysisResult(status="validation_error", operation_type=operation_type,
                              operation_name=operation_name, error_code="INVALID_VARIABLES",
                              arguments=arguments)
    return AnalysisResult(
        status="success",
        operation_type=operation_type,
        operation_name=operation_name,
        fields=fields,
        paths=paths,
        max_depth=max_depth,
        selection_plan=plan,
        arguments=arguments,
    )


def _has_unsupported_directive(node) -> bool:
    return any(directive.name.value not in {"skip", "include"} for directive in node.directives or ())


def _check_directives(node) -> None:
    if _has_unsupported_directive(node):
        raise _UnsupportedDirective


def _selection_is_included(selection: SelectionNode, variables) -> bool:
    skip = get_directive_values(GraphQLSkipDirective, selection, variables)
    if skip is not None and skip["if"]:
        return False
    include = get_directive_values(GraphQLIncludeDirective, selection, variables)
    return include is None or include["if"]


def _collect_fields(
    schema: GraphQLSchema,
    root_type: GraphQLObjectType,
    selection_set: SelectionSetNode,
    fragments: dict[str, FragmentDefinitionNode],
    variables,
    argument_variables,
) -> tuple[
    tuple[FieldReference, ...],
    SelectionScope,
    tuple[tuple[FieldReference, ...], ...],
    int | None,
    tuple[ArgumentUsage, ...] | None,
]:
    """Collect unique coordinates, a response plan, and bounded canonical paths."""
    fields: dict[FieldReference, None] = {}
    paths: dict[tuple[FieldReference, ...], None] = {}
    object_types_by_name: dict[str, frozenset[GraphQLObjectType]] = {}
    visited_selections = 0
    max_depth = 0
    paths_available = True
    arguments_available = True
    argument_visits = 0
    argument_states: dict[tuple[FieldReference, str], str] = {}

    def possible_objects(graphql_type: GraphQLCompositeType) -> frozenset[GraphQLObjectType]:
        if graphql_type.name not in object_types_by_name:
            if isinstance(graphql_type, GraphQLObjectType):
                object_types_by_name[graphql_type.name] = frozenset({graphql_type})
            else:
                object_types_by_name[graphql_type.name] = frozenset(schema.get_possible_types(graphql_type))
        return object_types_by_name[graphql_type.name]

    def walk(
        parent_type: GraphQLCompositeType,
        selections: SelectionSetNode,
        possible_parent_objects: frozenset[GraphQLObjectType],
        selection_depth: int,
        parent_path: tuple[FieldReference, ...],
        visited_fragments: set[tuple[str, frozenset[GraphQLObjectType]]],
    ) -> tuple[PlannedField, ...]:
        nonlocal visited_selections, max_depth, paths_available, arguments_available, argument_visits
        if selection_depth > MAX_SELECTION_DEPTH:
            raise _AnalysisLimitExceeded
        planned: list[PlannedField] = []
        applicable_types = frozenset(obj.name for obj in possible_parent_objects)
        for selection in selections.selections:
            visited_selections += 1
            if visited_selections > MAX_SELECTION_VISITS:
                raise _AnalysisLimitExceeded
            _check_directives(selection)
            if not _selection_is_included(selection, variables):
                continue

            if isinstance(selection, FieldNode):
                field_name = selection.name.value
                response_key = (selection.alias or selection.name).value
                if field_name.startswith("__"):
                    if field_name == "__typename":
                        planned.append(PlannedField(None, response_key, applicable_types))
                    continue
                coordinate = FieldReference(parent_type.name, field_name)
                fields[coordinate] = None
                path = parent_path + (coordinate,)
                if paths_available and path not in paths:
                    if len(paths) >= MAX_PATHS_PER_REQUEST:
                        # Optional analytics must not invalidate selected fields
                        # or the response plan. Discard incomplete path data so
                        # existing availability reporting stays truthful.
                        paths_available = False
                        paths.clear()
                    else:
                        paths[path] = None
                max_depth = max(max_depth, len(path))
                definition = parent_type.fields[field_name]
                if arguments_available:
                    supplied = {arg.name.value: arg.value for arg in selection.arguments or ()}
                    for argument_name in definition.args:
                        argument_visits += 1
                        key = (coordinate, argument_name)
                        if (len(argument_name) > 255 or argument_visits > MAX_ARGUMENT_VISITS
                                or (key not in argument_states and len(argument_states) >= MAX_ARGUMENTS_PER_REQUEST)):
                            arguments_available = False
                            argument_states.clear()
                            break
                        node = supplied.get(argument_name)
                        if node is None:
                            state = "omitted"
                        elif isinstance(node, VariableNode):
                            name = node.name.value
                            if argument_variables is None:
                                state = "unknown"
                            elif name not in argument_variables:
                                state = "omitted"
                            else:
                                state = "explicit_null" if argument_variables[name] is None else "provided"
                        else:
                            state = "explicit_null" if isinstance(node, NullValueNode) else "provided"
                        previous = argument_states.get(key, state)
                        # Unknown dominates: even a mixed claim would overstate
                        # certainty. Otherwise differing aliases are one mixed hit.
                        argument_states[key] = ("unknown" if "unknown" in (previous, state)
                                                else state if previous == state else "mixed")
                child = None
                if selection.selection_set is not None:
                    child_type = get_named_type(definition.type)
                    if is_composite_type(child_type):
                        child_objects = possible_objects(child_type)
                        # Fragment de-duplication is local to a response object.
                        # The same spread at another alias/field must retain its
                        # own plan even though its schema coordinates are shared.
                        child = SelectionScope(
                            frozenset(obj.name for obj in child_objects),
                            walk(child_type, selection.selection_set, child_objects,
                                 selection_depth + 1, path, set()),
                        )
                wrapper, list_depth = definition.type, 0
                while isinstance(wrapper, (GraphQLList, GraphQLNonNull)):
                    if isinstance(wrapper, GraphQLList):
                        list_depth += 1
                    wrapper = wrapper.of_type
                planned.append(PlannedField(coordinate, response_key, applicable_types, child, list_depth))
                continue

            spread_name = selection.name.value if isinstance(selection, FragmentSpreadNode) else None
            fragment = fragments[spread_name] if spread_name is not None else selection
            type_condition = fragment.type_condition
            fragment_type = parent_type if type_condition is None else schema.get_type(type_condition.name.value)
            matching_objects = possible_parent_objects
            if fragment_type is not parent_type:
                # Preserve every outer restriction in both selected fields and
                # applicable_types; widening it would create false observations.
                matching_objects = possible_parent_objects & possible_objects(fragment_type)
                if not matching_objects:
                    continue
            if spread_name is not None:
                _check_directives(fragment)
                fragment_visit = (spread_name, matching_objects)
                if fragment_visit in visited_fragments:
                    continue
                visited_fragments.add(fragment_visit)
            # Fragment/type-condition expansion changes schema context, not
            # the selected-field depth or canonical field path.
            planned.extend(walk(fragment_type, fragment.selection_set, matching_objects,
                                selection_depth + 1, parent_path, visited_fragments))
        return tuple(planned)

    root_objects = possible_objects(root_type)
    plan = SelectionScope(frozenset(obj.name for obj in root_objects),
                          walk(root_type, selection_set, root_objects, 1, (), set()))
    arguments = (tuple(ArgumentUsage(coordinate, name, state)
                       for (coordinate, name), state in argument_states.items())
                 if arguments_available else None)
    return tuple(fields), plan, tuple(paths), max_depth if paths_available else None, arguments
