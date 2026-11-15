"""PostgreSQL persistence checks using owned read/write sessions for introspection."""
import uuid

import pytest
from graphql import build_schema, get_introspection_query, graphql_sync
from sqlalchemy import func, select

import app.services as services
from app.models import GraphQLService, SchemaField, SchemaSnapshot, SchemaType
from app.schema_collector import CollectedSchema, InvalidIntrospectionResponseError
from app.schema_parser import parse_sdl
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration
SDL = "type Query { users: [User!]! } type User { id: ID!, name: String! }"


async def test_introspection_persists_snapshot_and_does_not_save_failures(isolated_database, monkeypatch):
    sessions = isolated_database
    schema = build_schema(SDL)
    result = graphql_sync(schema, get_introspection_query())
    assert result.errors is None
    payload = {"data": result.data}
    collected = CollectedSchema(schema=schema, parsed=parse_sdl(SDL), introspection_json=payload)

    async def fake_collect_schema(target_url):
        if target_url.endswith("/broken"):
            raise InvalidIntrospectionResponseError("introspection failed")
        return collected
    monkeypatch.setattr(services, "collect_schema", fake_collect_schema)
    async with sessions() as session:
        service = await services.create_service(session, "introspection-" + uuid.uuid4().hex,
            "Introspection integration test", endpoint_url="https://graphql.example.test/graphql")
    snapshot_out = await services.import_introspection_schema(sessions, service.id)
    async with sessions() as session:
        snapshot = await session.get(SchemaSnapshot, snapshot_out.id)
        assert snapshot.source == "introspection"
        assert snapshot.schema_sdl == collected.parsed.canonical_sdl
        assert snapshot.introspection_json == payload
        types = (await session.scalars(select(SchemaType).where(SchemaType.snapshot_id == snapshot.id))).all()
        assert {row.name for row in types} >= {"Query", "User"}
        fields = (await session.execute(select(SchemaType.name, SchemaField.name, SchemaField.target_type_id)
            .join(SchemaField, SchemaField.type_id == SchemaType.id).where(SchemaType.snapshot_id == snapshot.id))).all()
        fields = {(type_name, name): target for type_name, name, target in fields}
        assert fields[("Query", "users")] is not None and ("User", "name") in fields
    duplicate = await services.import_introspection_schema(sessions, service.id)
    assert duplicate.id == snapshot_out.id
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SchemaSnapshot)
            .where(SchemaSnapshot.service_id == service.id)) == 1
        failed = await services.create_service(session, "failed-" + uuid.uuid4().hex, "Failed",
                                             endpoint_url="https://graphql.example.test/broken")
    with pytest.raises(InvalidIntrospectionResponseError):
        await services.import_introspection_schema(sessions, failed.id)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(SchemaSnapshot)
            .where(SchemaSnapshot.service_id == failed.id)) == 0


@pytest.mark.parametrize("failure", [False, True])
async def test_introspection_never_commits_or_rolls_back_caller_work(isolated_database, monkeypatch, failure):
    sessions = isolated_database
    async with sessions() as session:
        service = await services.create_service(session, "target", "Original",
                                               endpoint_url="https://93.184.216.34/graphql")
    schema = build_schema(SDL)
    result = graphql_sync(schema, get_introspection_query())
    collected = CollectedSchema(schema, parse_sdl(SDL), {"data": result.data})
    async def collect(_target):
        if failure:
            raise InvalidIntrospectionResponseError("introspection failed")
        return collected
    monkeypatch.setattr(services, "collect_schema", collect)
    async with sessions() as caller:
        # Dirty persistent object, unflushed insert, and already-flushed work all
        # belong to a different caller transaction and must remain untouched.
        existing = await caller.get(GraphQLService, service.id)
        flushed = GraphQLService(key="flushed", name="Pending flushed")
        caller.add(flushed)
        await caller.flush()
        existing.description = "Uncommitted caller edit"
        pending = GraphQLService(key="pending", name="Pending unflushed")
        caller.add(pending)
        if failure:
            with pytest.raises(InvalidIntrospectionResponseError):
                await services.import_introspection_schema(sessions, service.id)
        else:
            await services.import_introspection_schema(sessions, service.id)
        assert caller.in_transaction() and existing in caller.dirty and pending in caller.new
        assert existing.description == "Uncommitted caller edit"
        async with sessions() as observer:
            assert (await observer.get(GraphQLService, service.id)).description is None
            assert await observer.scalar(select(GraphQLService.id).where(GraphQLService.key.in_(["flushed", "pending"]))) is None
            assert await observer.scalar(select(func.count()).select_from(SchemaSnapshot)) == (0 if failure else 1)
        # Caller explicitly decides to persist its work; introspection did not.
        await caller.commit()
    async with sessions() as observer:
        assert (await observer.get(GraphQLService, service.id)).description == "Uncommitted caller edit"
        assert await observer.scalar(select(func.count()).select_from(GraphQLService)
                                     .where(GraphQLService.key.in_(["flushed", "pending"]))) == 2
