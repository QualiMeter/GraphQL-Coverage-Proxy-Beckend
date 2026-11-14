"""Unit tests for AST-based GraphQL query analysis."""

import pytest
from graphql import build_schema

from app.contracts import FieldReference, GraphQLQueryInput
from app.query_analyzer import analyze_query


SCHEMA = build_schema(
    """
    type Query {
      user(id: ID!): User
      orders: [Order!]!
      health: String!
    }
    type Mutation {
      updateUser(id: ID!, name: String!): User!
    }
    type User {
      id: ID!
      name: String!
      orders: [Order!]!
    }
    type Order {
      id: ID!
      total: Int!
    }
    """
)


def coordinates(result):
    return {(field.type_name, field.field_name) for field in result.fields}


def test_analyzes_nested_fields_lists_aliases_and_uses_schema_coordinates():
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(
            query="""
              query GetUser($id: ID!) {
                primary: user(id: $id) {
                  id
                  displayName: name
                  orders { id total }
                }
              }
            """,
            operation_name="GetUser",
            variables={"id": "u-1"},
        ),
    )

    assert result.status == "success"
    assert result.operation_type == "query"
    assert result.operation_name == "GetUser"
    assert coordinates(result) == {
        ("Query", "user"),
        ("User", "id"),
        ("User", "name"),
        ("User", "orders"),
        ("Order", "id"),
        ("Order", "total"),
    }
    assert len(result.fields) == len(coordinates(result))


def test_selects_only_operation_named_in_request():
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(
            query="query A { health } query B { orders { total } }",
            operation_name="B",
        ),
    )

    assert result.status == "success"
    assert result.operation_name == "B"
    assert coordinates(result) == {("Query", "orders"), ("Order", "total")}


@pytest.mark.parametrize(
    ("query", "operation_name", "error_code"),
    [
        ("query {", None, "GRAPHQL_PARSE_ERROR"),
        ("query { missing }", None, "GRAPHQL_VALIDATION_ERROR"),
        ("query A { health } query B { orders { id } }", None, "OPERATION_NAME_REQUIRED"),
        ("query A { health }", "Missing", "OPERATION_NOT_FOUND"),
    ],
)
def test_invalid_requests_return_errors_without_partial_fields(query, operation_name, error_code):
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(query=query, operation_name=operation_name),
    )

    assert result.status in {"parse_error", "validation_error"}
    assert result.error_code == error_code
    assert result.fields == ()


def test_mutation_returns_mutation_root_and_nested_object_fields():
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(
            query="mutation Rename { updateUser(id: \"1\", name: \"Ada\") { id name } }"
        ),
    )

    assert result.status == "success"
    assert result.operation_type == "mutation"
    assert result.operation_name == "Rename"
    assert coordinates(result) == {
        ("Mutation", "updateUser"),
        ("User", "id"),
        ("User", "name"),
    }


def test_ambiguous_user_fields_are_distinguished_by_parent_type():
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(query="{ orders { id } user(id: \"1\") { id } }"),
    )

    assert result.status == "success"
    assert coordinates(result) == {
        ("Query", "orders"),
        ("Order", "id"),
        ("Query", "user"),
        ("User", "id"),
    }


@pytest.mark.parametrize(
    ("with_name", "expected"),
    [(True, {("Query", "user"), ("User", "id"), ("User", "name")}),
     (False, {("Query", "user"), ("User", "id")})],
)
def test_evaluates_include_directive_with_coerced_variables(with_name, expected):
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(
            query="query UserById($withName: Boolean!) { user(id: \"1\") { id name @include(if: $withName) } }",
            variables={"withName": with_name},
        ),
    )

    assert result.status == "success"
    assert coordinates(result) == expected


def test_skip_directive_omits_field_and_typename_is_not_counted():
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(query="query($skip: Boolean!) { user(id: \"1\") { __typename id @skip(if: $skip) } }", variables={"skip": True}),
    )

    assert result.status == "success"
    assert coordinates(result) == {("Query", "user")}


@pytest.mark.parametrize(
    "query",
    [
        "query { user(id: \"1\") { ...UserFields } } fragment UserFields on User { id }",
        "query { user(id: \"1\") { ... on User { id } } }",
    ],
)
def test_fragments_are_explicitly_unsupported_without_partial_coverage(query):
    result = analyze_query(SCHEMA, GraphQLQueryInput(query=query))

    assert result.status == "unsupported"
    assert result.error_code == "UNSUPPORTED_SELECTION"
    assert result.fields == ()


def test_subscription_is_explicitly_unsupported():
    schema = build_schema(
        "type Query { health: String! } type Subscription { tick: Int! }"
    )

    result = analyze_query(
        schema,
        GraphQLQueryInput(query="subscription Tick { tick }"),
    )

    assert result.status == "unsupported"
    assert result.operation_type == "subscription"
    assert result.error_code == "UNSUPPORTED_OPERATION_TYPE"
    assert result.fields == ()


def test_invalid_runtime_variables_return_validation_error():
    result = analyze_query(
        SCHEMA,
        GraphQLQueryInput(
            query="query($id: ID!) { user(id: $id) { id } }",
            variables={},
        ),
    )

    assert result.status == "validation_error"
    assert result.error_code == "INVALID_VARIABLES"
    assert result.fields == ()


def test_field_reference_contract_is_used():
    result = analyze_query(SCHEMA, GraphQLQueryInput(query="{ health }"))

    assert result.fields == (FieldReference("Query", "health"),)
