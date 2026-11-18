"""Argument categories share the existing field/fragment traversal."""

import pytest
from graphql import build_schema

from app.contracts import GraphQLQueryInput
from app.query_analyzer import analyze_query

SDL = '''
enum Status { COMPLETED PENDING }
input Filter { secret: String }
type Query { orders(status: Status = PENDING, filter: Filter): [Order] health: Boolean }
type Mutation { orders(status: Status): [Order] }
type Order { id: ID children(status: Status): [Order] }
'''
SCHEMA = build_schema(SDL)


def analyze(query, variables=None, operation_name=None, *, unavailable=False):
    return analyze_query(SCHEMA, GraphQLQueryInput(query=query,
        variables=None if unavailable else variables or {}, operation_name=operation_name))


def states(result):
    assert result.arguments is not None
    return {(a.field.type_name, a.field.field_name, a.argument_name): a.state for a in result.arguments}


@pytest.mark.parametrize('query,variables,expected', [
    ('{ orders(status: COMPLETED) { id } }', {}, 'provided'),
    ('{ orders(status: null) { id } }', {}, 'explicit_null'),
    ('{ orders { id } }', {}, 'omitted'),
    ('query($s: Status) { orders(status: $s) { id } }', {'s': 'COMPLETED'}, 'provided'),
    ('query($s: Status) { orders(status: $s) { id } }', {'s': None}, 'explicit_null'),
    ('query($s: Status) { orders(status: $s) { id } }', {}, 'omitted'),
    ('query($s: Status = COMPLETED) { orders(status: $s) { id } }', {}, 'provided'),
    ('query($s: Status = null) { orders(status: $s) { id } }', {}, 'explicit_null'),
    ('query($s: Status = COMPLETED) { orders(status: $s) { id } }', {'s': None}, 'explicit_null'),
    ('query($s: Status!) { orders(status: $s) { id } }', {}, 'unknown'),
    ('query($s: Status) { orders(status: $s) { id } }', {'s': 'private-invalid-enum'}, 'unknown'),
])
def test_literal_and_variable_classification(query, variables, expected):
    result = analyze(query, variables)
    assert states(result)['Query', 'orders', 'status'] == expected
    assert states(result)['Query', 'orders', 'filter'] == 'omitted'
    if expected == 'unknown':
        assert result.status == 'validation_error' and result.fields == ()
    else:
        assert result.status == 'success'
    assert 'private-invalid-enum' not in repr(result)


def test_variables_not_available_are_unknown_without_claiming_coverage():
    result = analyze('query($s: Status) { orders(status: $s) { id } }', unavailable=True)
    assert states(result)['Query', 'orders', 'status'] == 'unknown'
    assert result.fields == () and result.status == 'validation_error'


@pytest.mark.parametrize('selection,expected', [
    ('orders(status: COMPLETED) { id } ...F', 'provided'),
    ('... on Query { orders(status: null) { id } }', 'explicit_null'),
    ('a: orders(status: COMPLETED) { id } b: orders(status: COMPLETED) { id }', 'provided'),
    ('a: orders(status: COMPLETED) { id } b: orders(status: null) { id }', 'mixed'),
    ('a: orders(status: COMPLETED) { id } b: orders { id }', 'mixed'),
    ('a: orders(status: null) { id } b: orders { id } c: orders(status: COMPLETED) { id }', 'mixed'),
])
def test_fragments_aliases_and_repetitions_are_one_category(selection, expected):
    fragment = ' fragment F on Query { ...G } fragment G on Query { orders(status: COMPLETED) { id } }' if '...F' in selection else ''
    result = analyze('{ ' + selection + ' }' + fragment)
    assert result.status == 'success'
    assert states(result)['Query', 'orders', 'status'] == expected
    assert len(result.arguments) == 2


def test_unknown_dominates_aliases_deterministically():
    for selections in ('a: orders(status: $s) { id } b: orders { id }',
                       'b: orders { id } a: orders(status: $s) { id }'):
        result = analyze('query($s: Status!) { ' + selections + ' }')
        assert states(result)['Query', 'orders', 'status'] == 'unknown'


@pytest.mark.parametrize('condition,expected', [(True, {}), (False, {('Query', 'orders', 'status'): 'provided', ('Query', 'orders', 'filter'): 'omitted'})])
def test_skip_include_and_nested_fragments(condition, expected):
    result = analyze('query($hide: Boolean!) { ...F @skip(if: $hide) } '
                     'fragment F on Query { ... on Query @include(if: true) { orders(status: COMPLETED) { id } } }', {'hide': condition})
    assert states(result) == expected


def test_unknown_directive_selection_is_unavailable():
    result = analyze('query($s: Status, $show: Boolean!) { orders(status: $s) @include(if: $show) { id } }')
    assert result.arguments is None and result.fields == ()


def test_nested_field_coordinates_and_input_values_are_not_retained():
    result = analyze('{ orders(filter: {secret: "private-literal"}) { children(status: COMPLETED) { id } } }')
    assert states(result)['Order', 'children', 'status'] == 'provided'
    assert states(result)['Query', 'orders', 'filter'] == 'provided'
    assert 'private-literal' not in repr(result)


def test_operation_name_selects_only_mutation():
    result = analyze('query Read { orders(status: COMPLETED) { id } } mutation Write { orders(status: null) { id } }', operation_name='Write')
    assert states(result) == {('Mutation', 'orders', 'status'): 'explicit_null'}


@pytest.mark.parametrize('query', ['{ orders(noSuchArgument: "private-value") { id } }',
                                 '{ orders(status:', 'query A { health } query B { health }'])
def test_invalid_or_ambiguous_documents_are_unavailable(query):
    result = analyze(query)
    assert result.arguments is None and result.fields == ()
    assert 'private-value' not in repr(result)


def test_argument_record_limit_preserves_schema_coverage_and_plan(monkeypatch):
    from app import query_analyzer
    query = '{ orders { children { id } } }'
    normal = analyze(query)
    monkeypatch.setattr(query_analyzer, 'MAX_ARGUMENTS_PER_REQUEST', 1)
    limited = analyze(query)
    assert limited.arguments is None
    assert limited.fields == normal.fields and limited.selection_plan == normal.selection_plan
    assert limited.status == normal.status == 'success' and limited.paths == normal.paths


def test_repeated_argument_visit_limit_preserves_coverage(monkeypatch):
    from app import query_analyzer
    query = '{ a: orders { id } b: orders { id } }'
    normal = analyze(query)
    monkeypatch.setattr(query_analyzer, 'MAX_ARGUMENT_VISITS', 2)
    limited = analyze(query)
    assert limited.arguments is None and normal.arguments is not None
    assert limited.fields == normal.fields and limited.status == 'success'


def test_long_schema_argument_name_does_not_break_coverage():
    name = 'a' * 256
    schema = build_schema('type Query { field(' + name + ': String): String }')
    result = analyze_query(schema, GraphQLQueryInput(query='{ field }'))
    assert result.status == 'success' and len(result.fields) == 1 and result.arguments is None


def test_openapi_argument_contract_and_response_example():
    from app.main import app
    schema = app.openapi()
    route = schema['paths']['/api/runs/{run_id}/arguments']['get']
    parameters = {p['name']: p['schema'] for p in route['parameters']}
    assert {'typeName','fieldName','argumentName','limit','offset'} <= parameters.keys()
    assert parameters['limit']['maximum'] == 200 and parameters['offset']['minimum'] == 0
    assert route['security']
    model = schema['components']['schemas']['RunArgumentsOut']
    assert model['properties']['availability']['enum'] == ['available','partial','unavailable']
    example = model['examples'][0]
    assert example['availability'] == 'partial' and example['items'][0]['mixedCount'] == 1
    assert not {'variables','value','defaultValue'} & schema['components']['schemas']['ArgumentUsageOut']['properties'].keys()


def test_literal_directive_with_unavailable_variables_still_classifies_arguments():
    result = analyze('query($s: Status) { orders(status: $s) @include(if: true) { id } }', unavailable=True)
    assert result.status == 'validation_error' and result.fields == ()
    assert states(result)['Query', 'orders', 'status'] == 'unknown'
