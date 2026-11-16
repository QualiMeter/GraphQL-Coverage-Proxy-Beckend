import pytest
from graphql import parse, print_ast
from graphql.language import Visitor, visit

from app import safe_document
from app.safe_document import normalize_document


class Shape(Visitor):
    def __init__(self):
        super().__init__()
        self.shape = []

    def enter(self, node, *_):
        self.shape.append((node.kind, node.value if node.kind == 'name' else None))


def shape(text):
    visitor = Shape()
    visit(parse(text, no_location=True), visitor)
    return visitor.shape


DOCUMENT = '''
# comment-cookie-secret
query Fetch($input: Payload = {password: "default-secret", flags: [SECRET_ENUM, 92837]}, $flag: Boolean = true)
  @tag(value: "operation-secret") {
  alias: account(input: {nested: [{token: "field-secret", ratio: 8273.541, enabled: false, blank: null}]})
    @include(if: $flag) { ...UserFields ... on User @tag(value: "inline-secret") { id } }
}
fragment UserFields on User @tag(value: "fragment-secret") {
  name(arg: "argument-secret")
  ...More @tag(value: "spread-secret")
}
fragment More on User { id }
mutation Change { set(value: "mutation-secret") }
subscription Watch { changed(arg: "subscription-secret") }
'''


def test_real_document_structure_names_fragments_directives_defaults_are_preserved():
    safe = normalize_document(DOCUMENT)
    assert safe.availability == 'redacted'
    assert shape(safe.text) == shape(DOCUMENT)
    for value in ('comment-cookie-secret', 'default-secret', 'SECRET_ENUM', '92837', 'field-secret',
                  '8273.541', 'inline-secret', 'fragment-secret', 'argument-secret', 'spread-secret',
                  'operation-secret', 'mutation-secret', 'subscription-secret'):
        assert value not in safe.text
    assert '[REDACTED]' in safe.text and 'REDACTED' in safe.text
    assert '0.0' in safe.text and 'false' in safe.text
    assert '$input' in safe.text and 'alias: account' in safe.text
    assert print_ast(parse(safe.text)) == safe.text
    assert normalize_document(safe.text).text == safe.text


@pytest.mark.parametrize('source', ['{ account { id } }', 'query Named($id: ID!) { alias: account(id: $id) { id } }',
                                   '{ account(input: {}, flags: []) { id } }'])
def test_without_scalar_literals_available_and_comments_never_retained(source):
    result = normalize_document('# secret-comment\n' + source)
    assert result.availability == 'available'
    assert result.text == print_ast(parse(source))
    assert 'secret-comment' not in result.text


@pytest.mark.parametrize('value', ['"string-secret"', '"""block-secret\nline"""', '94837482', '-48494',
                                 '98.23e2', 'ENUM_SECRET', 'true', 'false', 'null',
                                 '["list-secret", {x: "nested-secret"}]'])
def test_every_literal_category_in_arguments_defaults_and_directives(value):
    source = f'query Q($v: Input = {value}) @tag(v: {value}) {{ field(v: {value}) }}'
    result = normalize_document(source)
    assert result.availability == 'redacted'
    assert shape(source) == shape(result.text)
    if value not in ('false','null'):
        assert value not in result.text


@pytest.mark.parametrize('source', ['', 'query {', 'type Query { x: String }', 'query Q { field(arg: "broken-secret) }',
                                   '\ud800'])
def test_parse_failures_and_non_executable_definitions_fail_closed_without_source(source, caplog):
    assert normalize_document(source).text is None
    assert normalize_document(source).availability == 'unavailable'
    assert not caplog.text


@pytest.mark.parametrize('limit,value', [('MAX_DOCUMENT_INPUT_BYTES', 3), ('MAX_SAFE_DOCUMENT_BYTES', 3),
    ('MAX_AST_NODES', 3), ('MAX_AST_DEPTH', 2), ('MAX_DOCUMENT_TOKENS', 3)])
def test_resource_limits_fail_closed(monkeypatch, limit, value):
    monkeypatch.setattr(safe_document, limit, value)
    assert normalize_document('{ field(arg: "size-secret") }').availability == 'unavailable'


@pytest.mark.parametrize('error_type', [RuntimeError, ValueError])
def test_printer_failure_does_not_expose_exception_source(monkeypatch, caplog, error_type):
    def broken(_):
        raise error_type('raw-query-secret')
    monkeypatch.setattr(safe_document, 'print_ast', broken)
    assert normalize_document('{ field(arg: "raw-query-secret") }').text is None
    assert 'raw-query-secret' not in caplog.text
    record, = caplog.records
    assert record.diagnostic == {
        'event': 'coverage_internal_failure', 'stage': 'safe_document_mask',
        'exception_type': error_type.__name__,
    }
    assert record.exc_info is None


def test_non_executable_ast_node_fails_closed():
    node = parse('type Query { field: String }').definitions[0]
    assert safe_document.mask_value_ast(node).availability == 'unavailable'
