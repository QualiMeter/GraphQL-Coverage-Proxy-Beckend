"""The production callback must produce real AST results, not fixture coverage."""

import pytest

from app.analyzer_adapter import _schema_from_sdl, analyze_e2e_query
from app.contracts import FieldReference, GraphQLQueryInput


SCHEMA_SDL = "type Query { user: User } type User { id: ID! name: String! }"


async def test_production_adapter_uses_snapshot_and_directive_variables():
    result = await analyze_e2e_query(
        GraphQLQueryInput(
            query="query Fetch($withName: Boolean!) { user { id name @include(if: $withName) } }",
            operation_name="Fetch",
            variables={"withName": False},
        ),
        SCHEMA_SDL,
    )
    assert result.status == "success"
    assert result.operation_name == "Fetch"
    assert result.fields == (FieldReference("Query", "user"), FieldReference("User", "id"))


async def test_production_adapter_keeps_unsupported_fragments_empty():
    result = await analyze_e2e_query(
        GraphQLQueryInput(query="query { user { ... on User { id } } }"),
        SCHEMA_SDL,
    )
    assert result.status == "unsupported"
    assert result.fields == ()


def test_schema_cache_is_bounded_and_does_not_mix_snapshots():
    first = _schema_from_sdl(SCHEMA_SDL)
    assert first is _schema_from_sdl(SCHEMA_SDL)
    other = _schema_from_sdl("type Query { health: Boolean }")
    assert other is not first
    assert _schema_from_sdl.cache_info().maxsize == 32


async def test_bad_snapshot_sdl_is_not_treated_as_success():
    with pytest.raises(Exception):
        await analyze_e2e_query(GraphQLQueryInput(query="{ user { id } }"), "invalid SDL")
