"""Stage 4 contracts against real PostgreSQL, including ACL and historic migration."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, func, select, text

from app.auth_models import ServiceGrant
from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.database import get_session
from app.main import app
from app.models import GraphQLService, ObservedRequest, RequestFieldUsage, SchemaField, SchemaType, TestRun as Run
from app.run_analytics import get_run_stats, operation_key
from app.services import create_run, create_service, finish_run, import_sdl, record_observation
from test_postgres_integration import postgres_session  # noqa: F401
from test_browser_auth_postgres import auth_context, grant, login, TEST_KEY  # noqa: F401
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration
AT = datetime(2026, 11, 15, tzinfo=timezone.utc)
SDL = 'type Query { users: [User!]! health: String } type User { id: ID! name: String }'


async def observation(session, run, *, hash='a'*64, name='Same', kind='query', status='success',
                      data=True, errors=False, http=200, duration=10, fields=(), at=AT):
    ident = await record_observation(session, RequestObservationInput(
        run_id=run.id, query_hash=hash, analysis=AnalysisResult(status=status, operation_name=name,
            operation_type=kind, fields=tuple(FieldReference(*f) for f in fields)),
        http_method='POST', http_status=http, response_has_data=data,
        graphql_has_errors=errors, duration_ms=duration,
    ))
    row = await session.get(ObservedRequest, ident)
    row.created_at = at
    await session.commit()
    return str(ident)


@pytest.fixture
async def journal_context(postgres_session, monkeypatch):
    session = postgres_session
    monkeypatch.setenv('COVERAGE_API_KEY', TEST_KEY)
    service = await create_service(session, 'history-' + uuid.uuid4().hex, 'History')
    schema = await import_sdl(session, service.id, SDL)
    other_schema = await import_sdl(session, service.id, SDL.replace('health: String', 'health: Int'))
    runs = []
    for index, snapshot in enumerate((schema, schema, other_schema), 1):
        row = Run(id=uuid.UUID(int=index), snapshot_id=snapshot.id, name=f'Run {index}',
                  status='completed', started_at=AT, finished_at=AT,
                  metadata_json={'secret': 'admin-only'})
        session.add(row)
        runs.append(row)
    await session.commit()
    active = await create_run(session, schema.id, 'Active', {})
    active_row = await session.get(Run, active.id)
    active_row.started_at = AT + timedelta(seconds=1)
    await session.commit()
    second = await create_service(session, 'isolated-' + uuid.uuid4().hex, 'Isolated')
    second_schema = await import_sdl(session, second.id, SDL)
    second_run = await create_run(session, second_schema.id, 'Other service', {})

    async def dependency():
        yield session
    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://graphql.rebyte.fun',
                                     headers={'X-Coverage-Api-Key': TEST_KEY}) as client:
            yield client, service, schema, other_schema, runs, active, second, second_run
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_service_snapshot_history_totals_dates_and_stable_pages(journal_context):
    client, service, schema, other_schema, runs, active, second, second_run = journal_context
    route = '/api/runs'
    response = await client.get(route, params={'serviceId': str(service.id)})
    assert response.status_code == 200, response.text
    ids = [row['id'] for row in response.json()]
    assert ids == [str(active.id), *[str(r.id) for r in reversed(runs)]]
    assert response.headers['X-Total-Count'] == '4'
    assert {row['snapshotId'] for row in response.json()} == {str(schema.id), str(other_schema.id)}
    assert response.json()[1]['metadata'] == {'secret': 'admin-only'}
    pages = [await client.get(route, params={'serviceId': str(service.id), 'limit': 2, 'offset': offset}) for offset in (0, 2, 4)]
    assert [row['id'] for page in pages for row in page.json()] == ids
    assert all(p.headers['X-Total-Count'] == '4' for p in pages)
    assert (await client.get(route, params={'snapshotId': str(schema.id)})).headers['X-Total-Count'] == '3'
    other = await client.get(route, params={'serviceId': str(second.id)})
    assert [r['id'] for r in other.json()] == [str(second_run.id)]
    bounds = {'serviceId': str(service.id), 'from': '2026-11-15T05:00:00+05:00', 'to': '2026-11-15T00:00:01Z'}
    bounded = await client.get(route, params=bounds)
    assert bounded.headers['X-Total-Count'] == '3'
    assert {r['id'] for r in bounded.json()} == {str(r.id) for r in runs}
    bounded = await client.get(route, params={**bounds, 'status': 'running'})
    assert bounded.json() == [] and bounded.headers['X-Total-Count'] == '0'
    running = await client.get(route, params={'serviceId': str(service.id), 'status': 'running'})
    assert [r['id'] for r in running.json()] == [str(active.id)]


async def test_unpaginated_snapshot_history_does_not_truncate(journal_context, postgres_session):
    client, service, schema, *_ = journal_context
    for _ in range(501):
        postgres_session.add(Run(snapshot_id=schema.id, name='Historic', status='completed', metadata_json={}))
    await postgres_session.commit()
    response = await client.get('/api/runs', params={'snapshotId': str(schema.id)})
    assert len(response.json()) == 504
    assert response.headers['X-Total-Count'] == '504'


async def test_empty_history(journal_context, postgres_session):
    client, service, *_ = journal_context
    snapshot = await import_sdl(postgres_session, service.id, 'type Query { empty: ID }')
    response = await client.get('/api/runs', params={'snapshotId': str(snapshot.id)})
    assert response.json() == [] and response.headers['X-Total-Count'] == '0'


@pytest.mark.parametrize('params', [
    {}, {'snapshotId': 'bad'}, {'serviceId': 'bad'},
    {'status': 'failed'}, {'status': 'bogus'}, {'limit': 0}, {'limit': 501}, {'offset': -1},
    {'from': '2026-11-15'}, {'from': '2026-11-15T00:00:00'},
    {'from': '2026-11-16T00:00:00Z', 'to': '2026-11-15T00:00:00Z'},
    {'from': '2026-11-15T00:00:00Z', 'to': '2026-11-15T00:00:00Z'},
    {'snapshotId': '00000000-0000-0000-0000-000000000001'},
])
async def test_history_validation(journal_context, params):
    client, service, *_ = journal_context
    supplied = params if params == {} else {'serviceId': str(service.id), **params}
    response = await client.get('/api/runs', params=supplied)
    assert response.status_code == 422, response.text
    assert 'X-Total-Count' not in response.headers


@pytest.fixture
async def journal_data(journal_context, postgres_session):
    client, service, schema, other_schema, runs, active, second, second_run = journal_context
    a = await observation(postgres_session, active, fields=(('Query', 'users'), ('User', 'id'), ('User', 'name')), duration=20, errors=True, http=500)
    b = await observation(postgres_session, active, hash='b'*64, fields=(('Query', 'health'),), duration=0)
    c = await observation(postgres_session, active, fields=(('User', 'id'),), duration=20, at=AT+timedelta(seconds=1))
    d = await observation(postgres_session, active, hash=None, name=None, kind=None, status='parse_error', data=None, errors=None, http=None, duration=None)
    e = await observation(postgres_session, active, kind='mutation', data=False, duration=5, http=201)
    foreign = await observation(postgres_session, second_run, fields=(('Query', 'health'),))
    return journal_context, (a, b, c, d, e), foreign


async def test_operation_identity_field_exists_and_combined_filters(journal_data):
    (client, _, _, _, _, run, *_), (a, b, c, d, e), _ = journal_data
    path = f'/api/runs/{run.id}/requests'
    key = operation_key('a'*64, 'Same', 'query')
    cases = [({'operationKey': key}, {a, c}),
             ({'operationName': 'Same'}, {a,b,c,e}), ({'operationName': 'same'}, set()),
             ({'typeName': 'User'}, {a,c}), ({'typeName': 'User', 'fieldName': 'name'}, {a}),
             ({'typeName': 'Query'}, {a,b}), ({'analysisStatus': 'parse_error'}, {d}),
             ({'graphqlHasErrors': False}, {b,c,e}), ({'graphqlHasErrors': True}, {a}),
             ({'httpStatus': 500, 'httpStatusClass': 5}, {a}), ({'httpStatus': 500, 'httpStatusClass': 2}, set()),
             ({'operationType': 'mutation'}, {e}),
             ({'from': '2026-11-15T05:00:00+05:00', 'to': '2026-11-15T00:00:01Z'}, {a,b,d,e}),
             ({'operationKey': key, 'operationName': 'Same', 'operationType': 'query', 'typeName': 'User',
               'fieldName': 'id', 'analysisStatus': 'success', 'graphqlHasErrors': True, 'httpStatus': 500,
               'httpStatusClass': 5, 'from': '2026-11-15T00:00:00Z', 'to': '2026-11-15T00:00:01Z'}, {a})]
    for params, expected in cases:
        response = await client.get(path, params=params)
        assert response.status_code == 200, response.text
        assert len(response.json()) == len(expected), params
        assert {r['id'] for r in response.json()} == expected, params
        assert response.headers['X-Total-Count'] == str(len(expected))
    legacy = await client.get(path, params={'limit': 2, 'offset': 1})
    assert len(legacy.json()) == 2 and legacy.headers['X-Total-Count'] == '5'
    assert next(r for r in (await client.get(path)).json() if r['id'] == d)['operationKey'] is None
    operations = (await client.get(f'/api/runs/{run.id}/operations')).json()
    assert key in {o['operationKey'] for o in operations}


@pytest.mark.parametrize('sort_by', ['createdAt', 'durationMs'])
@pytest.mark.parametrize('order', ['asc', 'desc'])
async def test_request_stable_sort_nulls_and_pages(journal_data, sort_by, order):
    (client, _, _, _, _, run, *_), ids, _ = journal_data
    path = f'/api/runs/{run.id}/requests'
    params = {'sortBy': sort_by, 'sortOrder': order}
    response = await client.get(path, params=params)
    rows = response.json()
    known = [r for r in rows if r[sort_by] is not None]
    assert known == sorted(known, key=lambda r: (r[sort_by], r['id']), reverse=order == 'desc')
    if sort_by == 'durationMs':
        assert rows[-1]['durationMs'] is None
    pages = [await client.get(path, params={**params, 'limit': 2, 'offset': offset}) for offset in (0,2,4,6)]
    assert [r['id'] for p in pages for r in p.json()] == [r['id'] for r in rows]
    assert len(set(r['id'] for r in rows)) == 5
    assert all(p.headers['X-Total-Count'] == '5' for p in pages)


@pytest.mark.parametrize('params', [
    {'operationKey': 'x'*64}, {'operationKey': 'a'*63}, {'operationType': 'bad'},
    {'analysisStatus': 'bad'}, {'graphqlHasErrors': 'bad'}, {'httpStatus': 99}, {'httpStatus': 600},
    {'httpStatusClass': 0}, {'httpStatusClass': 6}, {'sortBy': 'id'}, {'sortOrder': 'DESC'},
    {'fieldName': 'id'}, {'typeName': ''}, {'limit': 0}, {'limit': 501}, {'offset': -1},
    {'from': '2026-11-15'}, {'to': 'invalid'}, {'to': '2026-11-15T00:00:00'},
    {'from': '2026-11-15T01:00:00Z', 'to': '2026-11-15T00:00:00Z'},
])
async def test_request_filter_validation(journal_context, params):
    client, _, _, _, _, run, *_ = journal_context
    response = await client.get(f'/api/runs/{run.id}/requests', params=params)
    assert response.status_code == 422, response.text
    assert 'X-Total-Count' not in response.headers


async def test_detail_counting_snapshot_identity_missing_and_no_secrets(journal_data, postgres_session):
    (client, _, schema, other_schema, _, run, _, other_run), (a,b,c,d,e), foreign = journal_data
    path = f'/api/runs/{run.id}/requests'
    detail = await client.get(f'{path}/{a}')
    body = detail.json()
    assert detail.status_code == 200, detail.text
    assert body['operationKey'] == operation_key('a'*64, 'Same', 'query')
    assert body['countedInCoverage'] is True  # HTTP 500 and GraphQL errors still count data.
    assert body['selectedFields'] == [
        {'typeName': 'Query', 'fieldName': 'users', 'returnType': '[User!]!'},
        {'typeName': 'User', 'fieldName': 'id', 'returnType': 'ID!'},
        {'typeName': 'User', 'fieldName': 'name', 'returnType': 'String'},
    ]
    # Simulate inconsistent historic field references; never expose/filter other snapshots.
    wrong_field = await postgres_session.scalar(select(SchemaField.id).join(SchemaType, SchemaType.id == SchemaField.type_id)
        .where(SchemaType.snapshot_id == other_schema.id, SchemaField.name == 'health'))
    postgres_session.add(RequestFieldUsage(request_id=uuid.UUID(a), field_id=wrong_field))
    await postgres_session.commit()
    assert (await client.get(f'{path}/{a}')).json() == body
    filtered = await client.get(path, params={'typeName': 'Query', 'fieldName': 'health'})
    assert [r['id'] for r in filtered.json()] == [b]
    for ident in (foreign, str(uuid.uuid4())):
        response = await client.get(f'{path}/{ident}')
        assert response.status_code == 404 and response.json() == {'detail': 'Request not found'}
    assert (await client.get(f'/api/runs/{uuid.uuid4()}/requests/{a}')).status_code == 404
    assert (await client.get(f'{path}/bad')).status_code == 422
    total = 0
    for ident in (a,b,c,d,e):
        row = (await client.get(f'{path}/{ident}')).json()
        assert row['countedInCoverage'] == (row['analysisStatus'] == 'success' and row['responseHasData'] is True)
        total += row['countedInCoverage']
        assert not {'query', 'variables', 'cookies', 'responseBody', 'queryHash', 'safeGraphqlDocument'} & row.keys()
    assert total == (await get_run_stats(postgres_session, run.id)).counted_requests
    empty = (await client.get(f'{path}/{d}')).json()
    assert empty['selectedFields'] == [] and empty['operationKey'] is None


@pytest.mark.parametrize('role', [None, 'viewer', 'analyst', 'operator', 'admin', 'cli'])
async def test_history_journal_detail_acl_and_revocation(auth_context, postgres_session, role):
    client, users, private, public, private_schema, public_schema, private_run, public_run = auth_context
    request_ids = [await observation(postgres_session, run) for run in (private_run, public_run)]
    headers = {'X-Coverage-Api-Key': TEST_KEY} if role == 'cli' else {}
    if role not in (None, 'cli'):
        await login(client, role)
    privileged = role in ('admin', 'cli')
    for service, run, ident in zip((private, public), (private_run, public_run), request_ids):
        history = await client.get('/api/runs', params={'serviceId': str(service.id)}, headers=headers)
        assert history.status_code == (200 if privileged or service.is_public else 404)
        if history.status_code == 200:
            assert history.json()[0]['metadata'] == (run.metadata if privileged else {})
        else:
            assert 'X-Total-Count' not in history.headers
        for suffix in ('', '/' + ident):
            denied = await client.get(f'/api/runs/{run.id}/requests{suffix}', headers=headers)
            assert denied.status_code == (200 if privileged else 401 if role is None else 403 if role == 'viewer' else 404)
            if denied.status_code != 200:
                assert 'X-Total-Count' not in denied.headers
    if role not in (None, 'admin', 'cli'):
        for service in (private, public):
            await grant(postgres_session, users[role], service)
        for run, ident in zip((private_run, public_run), request_ids):
            for suffix in ('', '/' + ident):
                response = await client.get(f'/api/runs/{run.id}/requests{suffix}')
                assert response.status_code == (403 if role == 'viewer' else 200)
        await postgres_session.execute(delete(ServiceGrant).where(ServiceGrant.user_id == users[role].id))
        await postgres_session.commit()
        if role != 'viewer':
            for run, ident in zip((private_run, public_run), request_ids):
                for suffix in ('', '/' + ident):
                    denied = await client.get(f'/api/runs/{run.id}/requests{suffix}')
                    assert denied.status_code == 404 and 'X-Total-Count' not in denied.headers
    public_row = await postgres_session.get(GraphQLService, public.id)
    public_row.is_public = False
    await postgres_session.commit()
    history = await client.get('/api/runs', params={'serviceId': str(public.id)}, headers=headers)
    assert history.status_code == (200 if privileged else 404)
    if not privileged:
        assert 'X-Total-Count' not in history.headers


async def test_migration_backfills_bounded_batches_without_changing_history(isolated_database):
    async with isolated_database() as session:
        service = await create_service(session, 'migration-history', 'Existing')
        schema = await import_sdl(session, service.id, SDL)
        run = await create_run(session, schema.id, 'Historic', {'preserved': True})
        url = session.bind.url.render_as_string(hide_password=False)
    async def migrate(*args):
        result = await asyncio.to_thread(subprocess.run,
            [str(Path(sys.executable).with_name('alembic')), *args],
            env={**os.environ, 'DATABASE_URL': url}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    await migrate('downgrade', '20261116_service_endpoints')
    name = 'Legacy\n"\\名字'
    async with isolated_database() as session:
        await session.execute(text('''INSERT INTO observed_requests
            (id,run_id,query_hash,operation_name,operation_type,http_method,analysis_status,response_has_data,duration_ms)
            SELECT gen_random_uuid(),:run_id,:hash,:name,'query','POST','success',true,7 FROM generate_series(1,1003)'''),
            {'run_id': run.id, 'hash': 'a'*64, 'name': name})
        await session.execute(text('''INSERT INTO observed_requests
            (id,run_id,http_method,analysis_status) VALUES (:id,:run_id,'POST','parse_error')'''),
            {'id': uuid.uuid4(), 'run_id': run.id})
        await session.commit()
    await migrate('upgrade', 'head')
    async with isolated_database() as session:
        assert await session.scalar(select(func.count()).select_from(ObservedRequest).where(
            ObservedRequest.operation_key == operation_key('a'*64, name, 'query'))) == 1003
        assert await session.scalar(select(func.count()).select_from(ObservedRequest).where(
            ObservedRequest.operation_key.is_(None))) == 1
        restored = await session.get(Run, run.id)
        assert restored.snapshot_id == schema.id and restored.metadata_json == {'preserved': True}
        assert (await session.get(GraphQLService, service.id)).endpoint_url is None
        row = await session.scalar(select(ObservedRequest).where(ObservedRequest.operation_key.is_not(None)))
        assert row.operation_name == name and row.query_hash == 'a'*64 and row.duration_ms == 7
        row.operation_name = None
        await session.commit()
        assert row.operation_key == operation_key('a'*64, None, 'query')


@pytest.mark.parametrize('status', ['success', 'parse_error', 'validation_error', 'unsupported', 'internal_error'])
@pytest.mark.parametrize('data', [True, False, None])
async def test_detail_counted_exactly_like_engine(journal_context, postgres_session, status, data):
    client, _, _, _, _, run, *_ = journal_context
    ident = await observation(postgres_session, run, status=status, data=data, http=500, errors=True)
    response = await client.get(f'/api/runs/{run.id}/requests/{ident}')
    assert response.status_code == 200
    assert response.json()['countedInCoverage'] == (status == 'success' and data is True)
    assert int(response.json()['countedInCoverage']) == (await get_run_stats(postgres_session, run.id)).counted_requests


async def test_missing_scopes_empty_journal_and_query_count(journal_context, postgres_session):
    from sqlalchemy import event
    client, service, schema, _, _, run, *_ = journal_context
    for scope in ('snapshotId', 'serviceId'):
        response = await client.get('/api/runs', params={scope: str(uuid.uuid4())})
        assert response.status_code == 404 and 'X-Total-Count' not in response.headers
    path = f'/api/runs/{run.id}/requests'
    response = await client.get(path)
    assert response.json() == [] and response.headers['X-Total-Count'] == '0'
    assert (await client.get('/api/runs/bad/requests')).status_code == 422
    assert (await client.get(f'/api/runs/{uuid.uuid4()}/requests')).status_code == 404
    for route in ('/api/runs', path):
        params = {'snapshotId': str(schema.id)} if route == '/api/runs' else {}
        response = await client.get(route, params={**params, 'from': '1730000000'})
        assert response.status_code == 422
    for _ in range(10):
        ident = await observation(postgres_session, run, fields=(('Query','users'), ('User','id'), ('User','name')))
    queries = []
    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    engine = postgres_session.bind.engine.sync_engine
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        response = await client.get(path, params={'typeName': 'User', 'limit': 3, 'offset': 2})
        assert len(response.json()) == 3 and response.headers['X-Total-Count'] == '10'
        # Two ACL reads, optional run load, count, page: independent of page size/fields.
        assert len(queries) <= 5
        assert any('EXISTS' in q for q in queries)
        assert any('LIMIT' in q and 'OFFSET' in q for q in queries)
        queries.clear()
        assert (await client.get(f'{path}/{ident}')).status_code == 200
        assert len(queries) == 4  # Two ACL reads + request + all selected fields.
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
