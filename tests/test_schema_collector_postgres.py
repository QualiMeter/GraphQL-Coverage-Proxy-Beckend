"""PostgreSQL persistence check for introspection snapshots."""

import os
import uuid

import pytest
from graphql import build_schema, get_introspection_query, graphql_sync
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.services as services
from app.models import SchemaField, SchemaSnapshot, SchemaType
from app.schema_collector import CollectedSchema, InvalidIntrospectionResponseError
from app.schema_parser import parse_sdl


SDL = """
type Query { users: [User!]! }
type User { id: ID!, name: String! }
"""


@pytest.mark.integration
async def test_introspection_persists_snapshot_and_does_not_save_failures(monkeypatch):
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL missing; PostgreSQL integration NOT verified")

    default_database_url = "postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage"
    configured_url = os.environ.get("DATABASE_URL", default_database_url)
    if make_url(test_url).database == make_url(configured_url).database:
        pytest.fail("TEST_DATABASE_URL must name a database separate from DATABASE_URL")

    schema = build_schema(SDL)
    result = graphql_sync(schema, get_introspection_query())
    assert result.errors is None
    payload = {"data": result.data}
    collected = CollectedSchema(
        schema=schema,
        parsed=parse_sdl(SDL),
        introspection_json=payload,
    )

    async def fake_collect_schema(target_url: str) -> CollectedSchema:
        if target_url.endswith("/broken"):
            raise InvalidIntrospectionResponseError("introspection failed")
        return collected

    monkeypatch.setattr(services, "collect_schema", fake_collect_schema)

    engine = create_async_engine(test_url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    service = await services.create_service(
                        session,
                        "introspection-" + uuid.uuid4().hex,
                        "Introspection integration test",
                    )
                    snapshot_out = await services.import_introspection_schema(
                        session, service.id, "https://graphql.example.test/graphql"
                    )
                    snapshot = await session.get(SchemaSnapshot, snapshot_out.id)
                    assert snapshot is not None
                    assert snapshot.source == "introspection"
                    assert snapshot.schema_sdl == collected.parsed.canonical_sdl
                    assert snapshot.introspection_json == payload

                    type_rows = (
                        await session.scalars(
                            select(SchemaType).where(
                                SchemaType.snapshot_id == snapshot_out.id
                            )
                        )
                    ).all()
                    assert {row.name for row in type_rows} >= {"Query", "User"}

                    field_rows = (
                        await session.execute(
                            select(SchemaType.name, SchemaField.name, SchemaField.target_type_id)
                            .join(SchemaField, SchemaField.type_id == SchemaType.id)
                            .where(SchemaType.snapshot_id == snapshot_out.id)
                        )
                    ).all()
                    fields = {
                        (type_name, field_name): target_id
                        for type_name, field_name, target_id in field_rows
                    }
                    assert ("Query", "users") in fields
                    assert fields[("Query", "users")] is not None
                    assert ("User", "name") in fields

                    duplicate = await services.import_introspection_schema(
                        session, service.id, "https://graphql.example.test/graphql"
                    )
                    assert duplicate.id == snapshot_out.id
                    snapshot_count = await session.scalar(
                        select(func.count()).select_from(SchemaSnapshot).where(
                            SchemaSnapshot.service_id == service.id
                        )
                    )
                    assert snapshot_count == 1

                    failed_service = await services.create_service(
                        session,
                        "failed-introspection-" + uuid.uuid4().hex,
                        "Failed introspection integration test",
                    )
                    with pytest.raises(InvalidIntrospectionResponseError):
                        await services.import_introspection_schema(
                            session,
                            failed_service.id,
                            "https://graphql.example.test/broken",
                        )
                    failed_snapshot_count = await session.scalar(
                        select(func.count()).select_from(SchemaSnapshot).where(
                            SchemaSnapshot.service_id == failed_service.id
                        )
                    )
                    assert failed_snapshot_count == 0
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
