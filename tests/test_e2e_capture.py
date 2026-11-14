"""Pure request/response classification, without a running upstream or DB."""

import pytest

from app.e2e_capture import graphql_response_flags, parse_graphql_request


def test_request_parser_keeps_operation_and_variables_only_in_memory():
    query = 'query User($id: ID!) { user(id: $id) { id } }'
    parsed = parse_graphql_request(
        b'{"query":"query User($id: ID!) { user(id: $id) { id } }",'
        b'"operationName":"User","variables":{"id":"private-id"}}'
    )
    assert parsed is not None
    assert parsed.query == query
    assert parsed.operation_name == "User"
    assert parsed.variables == {"id": "private-id"}


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"",
        b"[]",
        b'{"query": 123}',
        b'{"query": ""}',
        b'{"query": "query { ok }", "variables": ["wrong-shape"]}',
        b'{"query": "query { ok }", "operationName": 12}',
        b'{"extensions":{"persistedQuery":{"sha256Hash":"hash"}}}',
    ],
)
def test_unsupported_request_body_does_not_produce_query_input(body):
    assert parse_graphql_request(body) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"data":{"user":{"id":"1"}}}', (True, False)),
        (b'{"data":null,"errors":[{"message":"private"}]}', (False, True)),
        (b'{"data":{}}', (True, False)),
        (b'{"data":"invalid"}', (False, False)),
        (b'{"errors":[{"message":"failed"}]}', (False, True)),
        (b'{"errors":[]}', (False, False)),
        (b"not-json", (None, None)),
        (b"[]", (None, None)),
        (None, (None, None)),
    ],
)
def test_response_flags_are_truthful_and_do_not_infer_success(body, expected):
    assert graphql_response_flags(body) == expected
