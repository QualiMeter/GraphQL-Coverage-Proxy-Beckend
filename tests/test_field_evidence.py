"""Presence classification of isolated selection plans."""
from dataclasses import replace

from graphql import build_schema
import pytest

from app.contracts import AnalysisResult, FieldReference as Ref, GraphQLQueryInput, PlannedField, SelectionScope
from app.field_evidence import match_field_evidence
from app.graphql_errors import response_envelope
from app.query_analyzer import analyze_query

SDL = '''
interface Node { id: ID! }
type User implements Node { id: ID! name: String friend: User }
type Order implements Node { id: ID! total: Int }
union Search = User | Order
type Query { user: User users: [User] matrix: [[User]] node: Node search: [Search] health: String }
'''
SCHEMA = build_schema(SDL)


def analyze(query, variables=None):
    result = analyze_query(SCHEMA, GraphQLQueryInput(query, variables=variables or {}))
    assert result.status == 'success'
    return result


def states(analysis, payload):
    result = match_field_evidence(analysis, payload)
    return None if result is None else {f'{e.field.type_name}.{e.field.field_name}': e.state for e in result}


@pytest.mark.parametrize('data,expected', [
    ({'health': 'private'}, 'observed'), ({'health': None}, 'observed'), ({}, 'not_observed'),
])
def test_selected_key_presence_including_null(data, expected):
    assert states(analyze('{ health }'), {'data': data}) == {'Query.health': expected}


@pytest.mark.parametrize('payload,root,child', [
    ({'data': {'user': {'name': 'Alex'}}}, 'observed', 'observed'),
    ({'data': {'user': {'name': None}}}, 'observed', 'observed'),
    ({'data': {'user': {}}}, 'observed', 'not_observed'),
    ({'data': {'user': None}}, 'observed', 'indeterminate'),
    ({'data': {}}, 'not_observed', 'indeterminate'),
    ({'data': {'user': 1}}, 'observed', 'indeterminate'),
    ({'data': {'user': {}}, 'errors': [{}]}, 'observed', 'indeterminate'),
    ({'data': {'user': {'name': None}}, 'errors': [{}]}, 'observed', 'observed'),
    ({'data': None, 'errors': [{}]}, 'indeterminate', 'indeterminate'),
    ({'errors': [{}]}, 'unavailable', 'unavailable'),
    ({'data': []}, 'unavailable', 'unavailable'),
    (None, 'unavailable', 'unavailable'),
])
def test_nested_null_missing_partial_and_unavailable(payload, root, child):
    assert states(analyze('{ user { name } }'), payload) == {'Query.user': root, 'User.name': child}


@pytest.mark.parametrize('value,expected', [
    ([], 'indeterminate'), (None, 'indeterminate'), ([None], 'indeterminate'),
    ([{}], 'not_observed'), ([{}, {}], 'not_observed'),
    ([{}, None], 'indeterminate'), ([{'name': None}, None, {}], 'observed'),
    ([{'name': 'x'}, {'name': 'y'}], 'observed'),
])
def test_lists_reduce_once_per_coordinate(value, expected):
    result = states(analyze('{ users { name } }'), {'data': {'users': value}})
    assert result == {'Query.users': 'observed', 'User.name': expected}


def test_nested_lists_and_merged_duplicate_selections():
    a = analyze('{ matrix { name } matrix { friend { name } } }')
    assert len(a.fields) == 3
    assert states(a, {'data': {'matrix': [[{'name': None, 'friend': {'name': None}}], []]}}) == {
        'Query.matrix': 'observed', 'User.name': 'observed', 'User.friend': 'observed'}


def test_aliases_and_repetitions_do_not_inflate_presence():
    a = analyze('{ a: user { n: name } b: user { name } a: user { n: name } }')
    assert states(a, {'data': {'a': {'n': None}, 'b': {}}}) == {
        'Query.user': 'observed', 'User.name': 'observed'}
    assert states(analyze('{ a: user { n: name } }'), {'data': {'user': {'name': 'x'}}}) == {
        'Query.user': 'not_observed', 'User.name': 'indeterminate'}


@pytest.mark.parametrize('variables,expected', [
    ({}, {'Query.user', 'User.name'}),
    ({'include': False}, {'Query.user'}),
    ({'skip': True}, {'Query.user'}),
])
def test_directives_use_coerced_values_and_defaults(variables, expected):
    a = analyze('query($include: Boolean! = true, $skip: Boolean! = false) { user { name @include(if: $include) @skip(if: $skip) } }', variables)
    assert set(states(a, {'data': {'user': {'name': 'x'}}})) == expected


def test_typename_is_in_memory_only_and_never_a_schema_coordinate():
    a = analyze('{ node { kind: __typename id } }')
    assert states(a, {'data': {'node': {'kind': 'User', 'id': None}}}) == {'Query.node': 'observed', 'Node.id': 'observed'}
    assert states(a, {'data': {'node': {'id': '1'}}})['Node.id'] == 'observed'
    assert all(f.field_name != '__typename' for f in a.fields)


# Isolated matcher fixtures; real AST fragment documents are tested separately
# in test_fragment_field_evidence.py and through the PostgreSQL proxy pipeline.
def fragment_plan(*, typename=True, list_depth=1, abstract='Search'):
    types = frozenset({'User', 'Order'})
    fields = [PlannedField(Ref('User', 'name'), 'value', frozenset({'User'})),
              PlannedField(Ref('Order', 'total'), 'value', frozenset({'Order'}))]
    if abstract == 'Node':
        fields.append(PlannedField(Ref('Node', 'id'), 'id', types))
    if typename:
        fields.append(PlannedField(None, 'kind', types))
    child = SelectionScope(types, tuple(fields))
    root = SelectionScope(frozenset({'Query'}), (PlannedField(Ref('Query', 'search'), 'search', frozenset({'Query'}), child, list_depth),))
    refs = tuple(f.coordinate for f in root.fields + child.fields if f.coordinate is not None)
    return AnalysisResult(status='success', fields=refs, selection_plan=root)


@pytest.mark.parametrize('abstract', ['Search', 'Node'])
@pytest.mark.parametrize('items,name,total', [
    ([{'kind': 'User', 'value': None}], 'observed', 'not_observed'),
    ([{'kind': 'User', 'value': 'Alex'}, {'kind': 'Order', 'value': 12}], 'observed', 'observed'),
    ([{'kind': 'Order', 'value': 12}], 'not_observed', 'observed'),
    ([{'value': 'no type'}], 'indeterminate', 'indeterminate'),
    ([{'kind': 'User', 'value': 'x'}, None], 'observed', 'indeterminate'),
    ([], 'indeterminate', 'indeterminate'),
    ([{'kind': 'Unknown', 'value': 'x'}], 'indeterminate', 'indeterminate'),
])
def test_named_inline_fragment_plan_contract_for_union_and_interface(abstract, items, name, total):
    result = states(fragment_plan(abstract=abstract), {'data': {'search': items}})
    assert result['User.name'] == name and result['Order.total'] == total
    assert result['Query.search'] == 'observed'


def test_unsolicited_typename_cannot_create_branch_evidence():
    result = states(fragment_plan(typename=False), {'data': {'search': [{'__typename': 'User', 'value': 'x'}]}})
    assert result['User.name'] == result['Order.total'] == 'indeterminate'


def test_conditional_typename_alias_cannot_discriminate_its_own_branch():
    a = fragment_plan(typename=False)
    f = a.selection_plan.fields[0]
    child = replace(f.child, fields=f.child.fields + (PlannedField(None, 'kind', frozenset({'User'})),))
    a = replace(a, selection_plan=replace(a.selection_plan, fields=(replace(f, child=child),)))
    assert states(a, {'data': {'search': [{'kind': 'User', 'value': 'x'}]}})['User.name'] == 'indeterminate'


@pytest.mark.parametrize('body', [b'bad', b'[]', b'{', b'{"data":{}}' + b' ' * 1_048_576, None])
def test_invalid_and_large_response_is_unavailable(body):
    payload, _, _ = response_envelope(body)
    assert states(analyze('{ health }'), payload) == {'Query.health': 'unavailable'}


def test_plan_missing_or_incomplete_is_unavailable_for_legacy_adapter():
    a = analyze('{ health }')
    assert match_field_evidence(replace(a, selection_plan=None), {'data': {'health': 1}}) is None
    assert match_field_evidence(replace(a, fields=()), {'data': {'health': 1}}) is None
    assert match_field_evidence(AnalysisResult(status='unsupported'), {}) is None


def test_budget_failure_discards_partial_evidence(monkeypatch):
    monkeypatch.setattr('app.field_evidence.MAX_MATCH_STEPS', 15)
    a = analyze('{ users { name } }')
    assert states(a, {'data': {'users': [{'name': 'x'}] * 100}}) == {
        'Query.users': 'unavailable', 'User.name': 'unavailable'}


def test_conflicting_typename_aliases_are_indeterminate():
    a = analyze('{ node { a: __typename b: __typename id } }')
    assert states(a, {'data': {'node': {'a': 'User', 'b': 'Order', 'id': '1'}}})['Node.id'] == 'indeterminate'


def test_plan_does_not_change_wire_input_or_retain_variables():
    request = GraphQLQueryInput('query($x: Boolean!) { user { n: name @include(if: $x) } }', variables={'x': True})
    before = repr(request)
    a = analyze_query(SCHEMA, request)
    assert repr(request) == before
    assert 'variables' not in repr(a.selection_plan)


@pytest.mark.parametrize('errors', [None, {}, 'bad', [None]])
def test_malformed_errors_do_not_establish_absence(errors):
    assert states(analyze('{ health }'), {'data': {}, 'errors': errors}) == {'Query.health': 'indeterminate'}


def test_invalid_root_data_is_unavailable():
    assert states(analyze('{ health }'), {'data': []}) == {'Query.health': 'unavailable'}


def test_empty_plan_has_no_fake_meta_field():
    a = analyze('{ search { __typename } }')
    assert states(a, {'data': {'search': [{'__typename': 'User'}]}}) == {'Query.search': 'observed'}
    a = analyze('{ __typename }')
    assert match_field_evidence(a, {'data': {'__typename': 'Query'}}) == ()
