import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import State

from app.analyzer_adapter import analyze_e2e_query
from app.contracts import AnalysisResult, ArgumentUsage, FieldReference, GraphQLQueryInput, RequestObservationInput
from app.main import app
from app.models import ObservedRequest, RequestArgumentUsage, SchemaField, SchemaType
from app.services import InvalidReferenceError, create_run, create_service, import_sdl, record_observation
from app.argument_usage import get_run_arguments
from test_argument_usage import SDL
from test_graphql_proxy import raw_response, upstream
from test_multi_service_postgres import isolated_database, routed_client
from test_browser_auth_postgres import auth_context, grant, login
from test_postgres_integration import postgres_session
from test_test_coverage_postgres import start

pytestmark = pytest.mark.integration


@pytest.fixture
async def context(isolated_database, routed_client, monkeypatch, upstream):
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    async with isolated_database() as session:
        service = await create_service(session, 'args-' + uuid.uuid4().hex, 'Arguments', endpoint_url='https://93.184.216.34/graphql')
        snapshot = await import_sdl(session, service.id, SDL)
        run = await create_run(session, snapshot.id, 'Arguments', {})
    calls = upstream(lambda request: raw_response(body=b'{"data":{"orders":[{"id":"1"}]}}'))
    return routed_client, isolated_database, str(run.id), calls


async def exchange(context, query, variables=None, **kwargs):
    client, _, run_id, _ = context
    return await client.post('/graphql', headers={'X-Coverage-Run-Id': run_id, **kwargs.pop('headers', {})},
        json={'query': query, 'variables': variables or {}, **kwargs})


async def report(context, **params):
    response = await context[0].get(f'/api/runs/{context[2]}/arguments', params={'typeName': 'Query', 'fieldName': 'orders', 'argumentName': 'status', **params})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('query,variables,state', [
    ('{ orders(status: COMPLETED) { id } }', {}, 'provided'),
    ('{ orders(status: null) { id } }', {}, 'explicitNull'),
    ('{ orders { id } }', {}, 'omitted'),
    ('query($s: Status) { orders(status: $s) { id } }', {'s': 'COMPLETED'}, 'provided'),
    ('query($s: Status) { orders(status: $s) { id } }', {'s': None}, 'explicitNull'),
    ('query($s: Status) { orders(status: $s) { id } }', {}, 'omitted'),
    ('query($s: Status = COMPLETED) { orders(status: $s) { id } }', {}, 'provided'),
    ('query($s: Status) { orders(status: $s) { id } }', {'s': 'secret-invalid-enum'}, 'unknown'),
    ('query($s: Status!) { orders(status: $s) { id } }', {}, 'unknown'),
    ('{ ...F } fragment F on Query { ...G } fragment G on Query { orders(status: COMPLETED) { id } }', {}, 'provided'),
    ('{ ... on Query { orders(status: null) { id } } }', {}, 'explicitNull'),
    ('{ a: orders(status: COMPLETED) { id } b: orders(status: COMPLETED) { id } }', {}, 'provided'),
    ('{ a: orders(status: COMPLETED) { id } b: orders(status: null) { id } }', {}, 'mixed'),
])
async def test_automatically_persists_one_category_per_http_request(context, query, variables, state):
    assert (await exchange(context, query, variables)).status_code == 200
    data = await report(context)
    assert data['availability'] == 'available' and data['analyzedRequests'] == 1
    assert data['snapshotId'] and data['items'][0]['argumentType'] == 'Status'
    if state == 'unknown':
        graph = (await context[0].get(f'/api/runs/{context[2]}/coverage')).json()
        assert graph['summary']['coveredFields'] == 0
    item = data['items'][0]
    assert item['hasSchemaDefault'] and item[state + 'Count'] == item['requestCount'] == 1
    assert sum(item[s + 'Count'] for s in ['provided','explicitNull','omitted','unknown','mixed']) == 1
    async with context[1]() as session:
        request = await session.scalar(select(ObservedRequest))
        assert request.test_execution_id is None and request.argument_usage_version == 1
        assert await session.scalar(select(func.count()).select_from(RequestArgumentUsage)) == 2


async def test_unknown_when_variables_are_unavailable_to_adapter(context, monkeypatch):
    async def unavailable(request, sdl):
        return await analyze_e2e_query(GraphQLQueryInput(query=request.query, variables=None), sdl)
    monkeypatch.setattr(app.state, 'coverage_analyzer', unavailable)
    assert (await exchange(context, 'query($s: Status) { orders(status: $s) { id } }', {'s': 'COMPLETED'})).status_code == 200
    assert (await report(context))['items'][0]['unknownCount'] == 1


async def test_parallel_http_exchanges_and_real_retries_count_separately(context, upstream):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def slow(request):
        nonlocal calls
        calls += 1
        if calls == 8:
            entered.set()
        await release.wait()
        return raw_response(body=b'{"data":{"orders":[]}}')
    upstream(slow)
    tasks = [asyncio.create_task(exchange(context, '{ orders(status: COMPLETED) { id } }')) for _ in range(8)]
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert context[1].kw['bind'].pool.checkedout() == 0
        assert (await context[0].post(f'/api/runs/{context[2]}/finish')).status_code == 409
    finally:
        release.set()
        responses = await asyncio.gather(*tasks)
    assert all(response.status_code == 200 for response in responses)
    assert (await exchange(context, '{ orders(status: COMPLETED) { id } }')).status_code == 200
    assert (await report(context))['items'][0]['providedCount'] == 9
    assert calls == 9


@pytest.mark.parametrize('body', [b'{"errors":[{"message":"private-error"}]}',
                                  b'{"data":{"orders":null},"errors":[{"message":"private-error","path":["orders"]}]}'])
async def test_graphql_errors_and_partial_results_do_not_claim_execution_success(context, upstream, body):
    upstream(lambda request: raw_response(body=body))
    assert (await exchange(context, '{ orders(status: COMPLETED) { id } }')).status_code == 200
    assert (await report(context))['items'][0]['providedCount'] == 1
    assert 'private-error' not in json.dumps(await report(context))


async def test_nonexistent_argument_is_unavailable_and_unknown_directives_do_not_guess(context):
    await exchange(context, '{ orders(noSuchArgument: "private") { id } }')
    await exchange(context, 'query($show: Boolean!) { orders @include(if: $show) { id } }')
    data = await report(context)
    assert data['availability'] == 'unavailable' and data['unavailableRequests'] == 2
    assert data['items'][0]['requestCount'] == 0
    assert not (await context[0].get(f'/api/runs/{context[2]}/arguments', params={'argumentName':'noSuchArgument'})).json()['items']


async def test_historical_partial_empty_and_unselected_fields(context):
    client, sessions, run_id, _ = context
    assert (await report(context))['availability'] == 'unavailable'
    async with sessions() as session:
        session.add(ObservedRequest(run_id=uuid.UUID(run_id), http_method='POST', analysis_status='success'))
        await session.commit()
    data = await report(context)
    assert data['availability'] == 'unavailable' and data['analyzedRequests'] == 0
    await exchange(context, '{ health }')
    data = await report(context)
    assert data['availability'] == 'partial' and data['unavailableRequests'] == data['analyzedRequests'] == 1
    assert data['items'][0]['omittedCount'] == data['items'][0]['requestCount'] == 0
    assert (await client.get(f'/api/runs/{run_id}/requests')).status_code == 200


async def test_sensitive_values_not_stored_in_database_journal_api_or_logs(context, caplog, capfd):
    client, sessions, run_id, calls = context
    query = 'query($f: Filter) { orders(filter: $f) { id } }'
    await exchange(context, query, {'f': {'secret':'private-variable-secret'}})
    await exchange(context, '{ orders(filter: {secret: "private-literal-secret"}) { id } }')
    await exchange(context, 'query($f: Filter = {secret: "private-default-secret"}) { orders(filter: $f) { id } }')
    async with sessions() as session:
        # Inspect all newly persisted request-linked columns, including masked AST.
        stored = (await session.execute(text('SELECT row_to_json(r)::text FROM observed_requests r'))).scalars().all()
        stored += (await session.execute(text('SELECT row_to_json(a)::text FROM request_argument_usage a'))).scalars().all()
    requests = (await client.get(f'/api/runs/{run_id}/requests')).json()
    details = [(await client.get(f'/api/runs/{run_id}/requests/{r["id"]}')).json() for r in requests]
    output = json.dumps([stored, requests, details, await report(context)]) + caplog.text + str(capfd.readouterr())
    for secret in ['private-variable-secret','private-literal-secret','private-default-secret']:
        assert secret not in output
    assert json.loads(calls[0].content)['variables']['f']['secret'] == 'private-variable-secret'


async def test_schema_and_test_coverage_semantics_unchanged(context):
    client, _, run_id, _ = context
    execution = (await start(client, run_id))[0]['id']
    await exchange(context, '{ orders(status: COMPLETED) { id } }', headers={'X-Coverage-Test-Execution-Id':execution})
    before = (await client.get(f'/api/runs/{run_id}/coverage')).json()['summary']
    await exchange(context, '{ orders(status: null) { id } }')
    await exchange(context, '{ orders { id } }')
    after = (await client.get(f'/api/runs/{run_id}/coverage')).json()['summary']
    assert before == after == {'coveredFields':2, 'totalFields':5, 'coveragePercent':40.0}
    summary = (await client.get(f'/api/runs/{run_id}/test-coverage')).json()
    assert summary['attributedRequests'] == 1 and summary['unattributedRequests'] == 2
    assert (await report(context))['items'][0]['requestCount'] == 3


async def test_filters_pagination_snapshot_and_database_uniqueness(context):
    client, sessions, run_id, _ = context
    await exchange(context, '{ orders(status: COMPLETED) { id } }')
    page = await client.get(f'/api/runs/{run_id}/arguments', params={'limit':1, 'offset':1})
    assert page.status_code == 200 and len(page.json()['items']) == 1 and page.json()['total'] == 4
    assert page.json()['offset'] == page.json()['limit'] == 1
    empty = await report(context, offset=999)
    assert empty['total'] == 1 and empty['items'] == [] and empty['totalRequests'] == 1
    for params in ({'limit':201}, {'offset':-1}, {'typeName':''}):
        assert (await client.get(f'/api/runs/{run_id}/arguments', params=params)).status_code == 422
    assert (await client.get(f'/api/runs/{uuid.uuid4()}/arguments')).status_code == 404
    async with sessions() as session:
        row = await session.scalar(select(RequestArgumentUsage))
        with pytest.raises(IntegrityError):
            await session.execute(insert(RequestArgumentUsage).values(request_id=row.request_id,
                field_id=row.field_id, argument_name=row.argument_name, state='provided'))
        await session.rollback()


@pytest.mark.parametrize('role,expected', [('viewer',403), ('analyst',200), ('operator',200), ('admin',200)])
async def test_roles_and_missing_service_grants(auth_context, postgres_session, role, expected):
    client, users, private, _, _, _, run, _ = auth_context
    await login(client, role)
    path = f'/api/runs/{run.id}/arguments'
    assert (await client.get(path)).status_code == (200 if role == 'admin' else 403 if role == 'viewer' else 404)
    await grant(postgres_session, users[role], private)
    assert (await client.get(path)).status_code == expected


async def test_public_service_does_not_publish_argument_usage(context):
    client, _, run_id, _ = context
    service = (await client.get('/api/services')).json()[0]
    await client.patch(f'/api/services/{service["id"]}', json={'isPublic':True})
    assert (await client.get(f'/api/runs/{run_id}/arguments', headers={'X-Coverage-Api-Key':''})).status_code == 401


async def test_storage_rejects_arguments_outside_snapshot_and_invalid_categories(context):
    _, sessions, run_id, _ = context
    for field, argument, state in [('orders','notAnArgument','provided'), ('orders','status','private-value'), ('notAField','status','provided')]:
        async with sessions() as session:
            with pytest.raises(InvalidReferenceError):
                await record_observation(session, RequestObservationInput(run_id=uuid.UUID(run_id),
                    analysis=AnalysisResult(status='success', arguments=(ArgumentUsage(FieldReference('Query', field), argument, state),)),
                    http_method='POST', http_status=200, response_has_data=True, graphql_has_errors=False))
            await session.rollback()


async def test_optional_argument_limit_does_not_replay_upstream_or_change_coverage(context, monkeypatch):
    from app import query_analyzer
    monkeypatch.setattr(query_analyzer, 'MAX_ARGUMENTS_PER_REQUEST', 1)
    await exchange(context, '{ orders { id } }')
    assert len(context[3]) == 1
    assert (await report(context))['availability'] == 'unavailable'
    graph = (await context[0].get(f'/api/runs/{context[2]}/coverage')).json()
    assert graph['summary']['coveredFields'] == 2


async def test_migration_round_trip_keeps_history_unavailable(context):
    _, sessions, run_id, _ = context
    async with sessions() as session:
        row = ObservedRequest(run_id=uuid.UUID(run_id), http_method='POST', analysis_status='success')
        session.add(row)
        await session.commit()
        ident = row.id
    engine = sessions.kw['bind']
    url = engine.url.render_as_string(hide_password=False)
    await engine.dispose()
    for args in [('downgrade','20261125_run_in_flight'), ('upgrade','head'), ('check',)]:
        result = await asyncio.to_thread(subprocess.run, [sys.executable,'-m','alembic',*args],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ,'DATABASE_URL':url}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    async with sessions() as session:
        assert (await session.get(ObservedRequest,ident)).argument_usage_version is None
        assert await session.scalar(select(func.count()).select_from(RequestArgumentUsage)) == 0
        assert (await get_run_arguments(session, uuid.UUID(run_id))).availability == 'unavailable'
