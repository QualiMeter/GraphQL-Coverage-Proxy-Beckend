"""Unit tests for Schema Collector using an in-memory HTTP transport."""

import httpx
import pytest
from graphql import build_schema, get_introspection_query, graphql_sync

from app.schema_collector import (
    InvalidIntrospectionResponseError,
    SchemaConnectionError,
    collect_schema,
)


TARGET_URL = "https://graphql.example.test/graphql"
SDL = """
type Query {
    users(limit: Int = 10): [User!]!
}
type Mutation {
    createUser(name: String!): User!
}
type User {
    id: ID!
    name: String!
}
"""


def introspection_payload() -> dict:
    result = graphql_sync(build_schema(SDL), get_introspection_query())
    assert result.errors is None
    return {"data": result.data}


@pytest.mark.asyncio
async def test_collect_schema_builds_graphql_schema_and_normalized_sdl():
    payload = introspection_payload()
    seen_requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json=payload)

    result = await collect_schema(
        TARGET_URL,
        transport=httpx.MockTransport(handle_request),
    )

    assert len(seen_requests) == 1
    assert seen_requests[0].method == "POST"
    assert "__schema" in seen_requests[0].read().decode("utf-8")
    assert result.schema.query_type.name == "Query"
    assert result.schema.mutation_type.name == "Mutation"
    assert result.schema.query_type.fields["users"].args["limit"].type.name == "Int"
    assert result.introspection_json == payload
    assert result.parsed.canonical_sdl
    assert len(result.parsed.sha256) == 64

    fields = {(field.type_name, field.name): field for field in result.parsed.fields}
    assert fields[("Query", "users")].target_type_name == "User"
    assert fields[("User", "name")].return_type == "String!"


@pytest.mark.asyncio
async def test_collect_schema_reports_upstream_http_errors():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"message": "unavailable"})
    )

    with pytest.raises(SchemaConnectionError, match="HTTP 503"):
        await collect_schema(TARGET_URL, transport=transport)


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "Introspection is disabled"}]},
        {"data": {}},
        {"data": {"__schema": {}}},
    ],
)
@pytest.mark.asyncio
async def test_collect_schema_rejects_graphql_and_invalid_schema_responses(payload: dict):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(InvalidIntrospectionResponseError):
        await collect_schema(TARGET_URL, transport=transport)


@pytest.mark.asyncio
async def test_collect_schema_rejects_non_json_responses():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"not-json")
    )

    with pytest.raises(InvalidIntrospectionResponseError, match="invalid JSON"):
        await collect_schema(TARGET_URL, transport=transport)
