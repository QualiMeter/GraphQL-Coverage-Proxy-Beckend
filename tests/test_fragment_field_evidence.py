"""Real fragment documents through the one AST traversal and JSON matcher."""
import pytest
from graphql import build_schema

from app import query_analyzer
from app.contracts import GraphQLQueryInput
from app.field_evidence import match_field_evidence
from app.query_analyzer import analyze_query

SDL = '''
interface Node { id: ID! }
union Search = User | Order
type User implements Node { id: ID! name: String friend: User }
type Order implements Node { id: ID! total: Int }
type Query { search: Search results: [Search] node: Node user: User health: String }
'''
SCHEMA = build_schema(SDL)
SEARCH = '''query Search { search { __typename ... on User { name } ... on Order { total } } }'''
NESTED = '''
query Search($skip: Boolean! = false, $include: Boolean! = true) {
  results {
    kind: __typename
    ...Common
    ...Common
    ... on User { ...Details @include(if: $include) }
    ... on Order { sum: total @skip(if: $skip) }
  }
}
fragment Common on Node { key: id }
fragment Details on User { display: name friend { ...Friend } }
fragment Friend on User { label: name }
'''


def evidence(query, payload, variables=None):
    request = GraphQLQueryInput(query, variables=variables or {})
    result = analyze_query(SCHEMA, request)
    assert result.status == 'success', result
    assert result.selection_plan is not None
    matched = match_field_evidence(result, payload)
    assert matched is not None
    assert {e.field for e in matched} == set(result.fields)
    return result, {f'{e.field.type_name}.{e.field.field_name}': e.state for e in matched}


@pytest.mark.parametrize('value,expected', [
    ({'__typename': 'User', 'name': 'Alex'}, ('observed', 'not_observed')),
    ({'__typename': 'User', 'name': None}, ('observed', 'not_observed')),
    ({'__typename': 'User'}, ('not_observed', 'not_observed')),
    ({'__typename': 'Order', 'total': 0}, ('not_observed', 'observed')),
    ({'name': 'Alex'}, ('indeterminate', 'indeterminate')),
    ({'__typename': 'Unknown', 'name': 'Alex'}, ('indeterminate', 'indeterminate')),
    ({'__typename': None, 'name': 'Alex'}, ('indeterminate', 'indeterminate')),
    (None, ('indeterminate', 'indeterminate')),
])
def test_real_inline_fragment_presence(value, expected):
    _, states = evidence(SEARCH, {'data': {'search': value}})
    assert (states['User.name'], states['Order.total']) == expected
    assert states['Query.search'] == 'observed'
    assert all('__typename' not in coordinate for coordinate in states)


@pytest.mark.parametrize('query', [
    '{ search { kind: __typename ...U ...O } } fragment U on User { name } fragment O on Order { total }',
    '{ search { kind: __typename ...Both } } fragment Both on Search { ...U ... on Order { total } } fragment U on User { name }',
    '{ search { kind: __typename ... on User { ... on Node { id ... on Order { total } } name } ... on Order { total } } }',
])
def test_named_nested_and_inline_fragments_keep_type_conditions(query):
    result, states = evidence(query, {'data': {'search': {'kind': 'User', 'name': None, 'id': 'x'}}})
    assert states['User.name'] == 'observed'
    assert states['Order.total'] == 'not_observed'
    child = result.selection_plan.fields[0].child
    assert child.possible_types == frozenset({'User', 'Order'})
    assert next(f for f in child.fields if f.coordinate and f.coordinate.field_name == 'name').applicable_types == frozenset({'User'})


@pytest.mark.parametrize('objects,expected', [
    ([{'kind': 'User', 'key': '1', 'display': None, 'friend': {'label': 'Alex'}},
      {'kind': 'Order', 'key': '2', 'sum': 10}], ('observed', 'observed', 'observed')),
    ([{'kind': 'User', 'key': '1', 'display': 'Alex', 'friend': None}], ('observed', 'not_observed', 'observed')),
    ([{'key': '1', 'display': 'Alex', 'sum': 10}], ('indeterminate', 'indeterminate', 'observed')),
    ([None, {'kind': 'Order', 'sum': 10}], ('indeterminate', 'observed', 'indeterminate')),
    ([], ('indeterminate', 'indeterminate', 'indeterminate')),
])
def test_nested_named_fragments_arrays_aliases_and_null(objects, expected):
    _, states = evidence(NESTED, {'data': {'results': objects}})
    assert (states['User.name'], states['Order.total'], states['Node.id']) == expected


def test_named_fragment_type_intersection_is_not_widened_by_interface():
    query = '{ user { ...Shared } } fragment Shared on Node { id ... on Order { total } }'
    result, states = evidence(query, {'data': {'user': {'id': None}}})
    assert states == {'Query.user': 'observed', 'Node.id': 'observed'}
    child = result.selection_plan.fields[0].child
    assert child.possible_types == frozenset({'User'})
    assert child.fields[0].applicable_types == frozenset({'User'})


@pytest.mark.parametrize('variables,selected', [
    ({}, {'Query.results', 'Node.id', 'User.name', 'User.friend', 'Order.total'}),
    ({'include': False}, {'Query.results', 'Node.id', 'Order.total'}),
    ({'skip': True}, {'Query.results', 'Node.id', 'User.name', 'User.friend'}),
    ({'skip': True, 'include': False}, {'Query.results', 'Node.id'}),
])
def test_fragment_and_field_directives_with_default_and_runtime_variables(variables, selected):
    _, states = evidence(NESTED, {'data': {'results': []}}, variables)
    assert set(states) == selected


def test_directives_on_spreads_inline_fragments_and_skipped_parents():
    query = '''query($skip: Boolean! = true) {
      a: user @skip(if: $skip) { ...Details }
      b: user { ...Details @skip(if: $skip) ... on User @include(if: false) { id } }
      c: user { ...Details @include(if: true) ...Details }
    } fragment Details on User { name }
    '''
    result, states = evidence(query, {'data': {'b': {}, 'c': {'name': None}}})
    assert states == {'Query.user': 'observed', 'User.name': 'observed'}
    assert [f.response_key for f in result.selection_plan.fields] == ['b', 'c']


def test_repeated_spread_retains_plan_at_each_alias_and_nested_object():
    query = '''{ first: user { ...Names } second: user { ...Names friend { ...Names } } }
               fragment Names on User { name }'''
    result, states = evidence(query, {'data': {'first': {}, 'second': {'name': None, 'friend': None}}})
    assert states == {'Query.user': 'observed', 'User.name': 'observed', 'User.friend': 'observed'}
    assert len(result.fields) == 3
    assert result.selection_plan.fields[0].child.fields[0].response_key == 'name'
    assert result.selection_plan.fields[1].child.fields[0].response_key == 'name'
    assert result.selection_plan.fields[1].child.fields[1].child.fields[0].response_key == 'name'


def test_repeated_spreads_in_one_response_object_are_deduplicated():
    result, _ = evidence('{ user { ...Names ...Names ... on User { ...Names } } } fragment Names on User { name }',
                         {'data': {'user': {'name': None}}})
    assert len(result.fields) == 2
    assert len(result.selection_plan.fields[0].child.fields) == 1


@pytest.mark.parametrize('value,expected', [
    ({'__typename': 'User'}, 'indeterminate'),
    ({'__typename': 'User', 'name': None}, 'observed'),
    (None, 'indeterminate'),
])
def test_partial_data_errors_keep_presence_and_uncertainty_separate(value, expected):
    _, states = evidence(SEARCH, {'data': {'search': value}, 'errors': [{'path': ['search', 'name']}]})
    assert states['User.name'] == expected
    assert states['Order.total'] == ('indeterminate' if value is None else 'not_observed')


def test_interface_common_field_can_be_observed_without_runtime_discriminator():
    _, states = evidence('{ node { ...Common ... on User { name } ... on Order { total } } } fragment Common on Node { id }',
                         {'data': {'node': {'id': None, 'name': 'x', 'total': 1}}})
    assert states['Node.id'] == 'observed'
    assert states['User.name'] == states['Order.total'] == 'indeterminate'


def test_typename_in_merged_selection_discriminates_other_selection_set():
    _, states = evidence('{ search { kind: __typename } search { ... on User { name } ... on Order { total } } }',
                         {'data': {'search': {'kind': 'User', 'name': 'Alex'}}})
    assert states['User.name'] == 'observed'
    assert states['Order.total'] == 'not_observed'


@pytest.mark.parametrize('query', [
    '{ health user { ...Missing } }',
    '{ health user { ...A } } fragment A on User { ...B } fragment B on User { ...A }',
    '{ health user { ... on Order { total } } }',
    'query($include: Boolean!) { health user { name @include(if: $include) } }',
])
def test_invalid_document_or_variables_has_no_partial_fields_or_plan(query):
    result = analyze_query(SCHEMA, GraphQLQueryInput(query))
    assert result.status == 'validation_error'
    assert result.fields == () and result.selection_plan is None
    assert match_field_evidence(result, {'data': {'health': 'ok'}}) is None


@pytest.mark.parametrize('limit,value', [('MAX_QUERY_TOKENS', 5), ('MAX_SELECTION_VISITS', 3), ('MAX_SELECTION_DEPTH', 2)])
def test_analyzer_complexity_limits_discard_both_outputs(limit, value, monkeypatch):
    monkeypatch.setattr(query_analyzer, limit, value)
    result = analyze_query(SCHEMA, GraphQLQueryInput('{ health user { ...Names } } fragment Names on User { name friend { name } }'))
    assert result.status in {'parse_error', 'unsupported'}
    assert result.fields == () and result.selection_plan is None


def test_typename_in_merged_nested_fragment_scopes_is_shared():
    schema = build_schema(SDL.replace('friend: User', 'friend: Node'))
    query = '{ user { friend { kind: __typename } } user { friend { ... on User { name } ... on Order { total } } } }'
    result = analyze_query(schema, GraphQLQueryInput(query))
    assert result.status == 'success'
    matched = match_field_evidence(result, {'data': {'user': {'friend': {'kind': 'User', 'name': None}}}})
    states = {f'{e.field.type_name}.{e.field.field_name}': e.state for e in matched}
    assert states['User.name'] == 'observed'
    assert states['Order.total'] == 'not_observed'


@pytest.mark.parametrize('obj,errors,expected', [({'id': None}, [], 'observed'), ({}, [], 'not_observed'), ({}, [{}], 'indeterminate')])
def test_common_coordinate_across_all_branches_needs_no_runtime_guess(obj, errors, expected):
    query = '{ search { ... on User { ...Common } ... on Order { ...Common } } } fragment Common on Node { id }'
    _, states = evidence(query, {'data': {'search': obj}, 'errors': errors})
    assert states['Node.id'] == expected


def test_explicit_typename_selected_in_every_branch_is_unconditional_together():
    query = '{ search { ... on User { kind: __typename name } ... on Order { kind: __typename total } } }'
    _, states = evidence(query, {'data': {'search': {'kind': 'User', 'name': None}}})
    assert states['User.name'] == 'observed'
    assert states['Order.total'] == 'not_observed'
