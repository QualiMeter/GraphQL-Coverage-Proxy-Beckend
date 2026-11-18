"""PostgreSQL checks for percentiles, safe wire AST, schema metadata and ACL."""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app import e2e_capture
from app.analyzer_adapter import analyze_e2e_query
from app.auth_models import ServiceGrant
from app.database import get_session, get_session_factory
from app.e2e_capture import capture_e2e_exchange
from app.main import app
from app.models import GraphQLService, ObservedRequest, SchemaField, SchemaSnapshot, SchemaType
from app.run_analytics import get_run_stats, operation_key
from app.services import create_run, create_service, finish_run, get_run_graph, import_sdl
from app.upstream import PinnedTransport
from test_postgres_integration import postgres_session  # noqa: F401
from test_run_analytics_postgres import analytics_run, observe  # noqa: F401
from test_browser_auth_postgres import auth_context, grant, login, TEST_KEY  # noqa: F401
from test_graphql_proxy import raw_response
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration
SDL = '''
schema { query: Read mutation: Write subscription: Events }
"Root description" type Read {
  "Field description" user("Input description" input: Credentials = {password: "schema-default-secret", modes: [ADMIN]}): User!
    @deprecated(reason: "Use node")
  node: Node
  search: [Search!]!
}
"Entity interface" interface Node { id: ID! }
"User description" type User implements Node { id: ID! name: String! }
type Team implements Node { id: ID! }
"Search description" union Search = Team | User
input Credentials { password: String! modes: [Mode!] nested: [Nested!] }
input Nested { token: String number: Int ratio: Float flag: Boolean }
enum Mode { ADMIN GUEST }
type Write { update(input: Credentials!): User! }
type Events { changed: User }
directive @tag(value: String) on QUERY | FIELD | FRAGMENT_DEFINITION | FRAGMENT_SPREAD | INLINE_FRAGMENT
'''


@pytest.fixture
async def feature_context(postgres_session, monkeypatch):
    monkeypatch.setenv('COVERAGE_API_KEY', TEST_KEY)
    service = await create_service(postgres_session, 'features-' + uuid.uuid4().hex, 'Features',
                                   endpoint_url='https://93.184.216.34/graphql')
    snapshot = await import_sdl(postgres_session, service.id, SDL)
    run = await create_run(postgres_session, snapshot.id, 'Features', {})
    async def dependency():
        yield postgres_session
    app.dependency_overrides[get_session] = dependency
    sessions = async_sessionmaker(bind=postgres_session.bind, expire_on_commit=False,
                                 join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_session_factory] = lambda: sessions
    from app.proxy import get_proxy_session
    app.dependency_overrides[get_proxy_session] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://graphql.rebyte.fun',
                                     headers={'X-Coverage-Api-Key': TEST_KEY}) as client:
            yield client, service, snapshot, run
    finally:
        app.dependency_overrides.pop(get_session_factory, None)
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_proxy_session, None)


async def test_all_analysis_categories_percentile_mean_timeline_and_run_isolation(postgres_session, analytics_run):
    service, schema, run = analytics_run
    statuses = ('success', 'parse_error', 'validation_error', 'unsupported', 'internal_error')
    for status, duration in zip(statuses, (0, None, 10, 20, 100)):
        await observe(postgres_session, run.id, status=status, duration=duration)
    stats = await get_run_stats(postgres_session, run.id)
    assert stats.total_requests == sum(stats.analysis_status_counts.model_dump().values()) == 5
    assert stats.analysis_status_counts.model_dump(by_alias=True) == {
        'success': 1, 'parseError': 1, 'validationError': 1, 'unsupported': 1, 'internalError': 1,
    }
    assert stats.p95_duration_ms == pytest.approx(88.0)
    assert stats.avg_duration_ms == 32.5
    assert stats.request_timeline[0].request_count == 5 and stats.request_timeline[0].avg_duration_ms == 32.5
    assert stats.counted_requests == 1
    await finish_run(postgres_session, run.id)
    later = await create_run(postgres_session, schema.id, 'Later', {})
    await observe(postgres_session, later.id, duration=99999)
    assert await get_run_stats(postgres_session, run.id) == stats
    assert (await get_run_stats(postgres_session, later.id)).p95_duration_ms == 99999


@pytest.mark.parametrize('durations,expected,average', [([],None,None), ([None],None,None),
    ([0],0,0), ([7,None],7,7), ([1,2,3,4],3.85,2.5)])
async def test_percentile_empty_single_null_and_interpolation(postgres_session, analytics_run, durations, expected, average):
    _, _, run = analytics_run
    for duration in durations:
        await observe(postgres_session, run.id, duration=duration)
    stats = await get_run_stats(postgres_session, run.id)
    assert stats.p95_duration_ms == (pytest.approx(expected) if expected is not None else None)
    assert stats.avg_duration_ms == average
    assert stats.total_requests == sum(stats.analysis_status_counts.model_dump().values()) == len(durations)


async def test_real_proxy_preserves_wire_and_identity_but_persists_only_safe_ast(feature_context, postgres_session, monkeypatch, caplog):
    client, service, snapshot, run = feature_context
    caplog.set_level(logging.DEBUG)
    calls = []
    async def upstream(self, request):
        calls.append(request)
        return raw_response(200, b'{"data":{"user":{"id":"response-secret","name":"Response"}}}')
    monkeypatch.setattr(PinnedTransport, 'handle_async_request', upstream)
    source = '''# raw-comment-secret
query Fetch($input: Credentials = {password: "default-runtime-secret", modes: [GUEST]}) @tag(value: "directive-runtime-secret") {
  alias: user(input: {password: "literal-runtime-secret", modes: [ADMIN], nested: [{token: "nested-runtime-secret", number: 987654, ratio: 876.543, flag: true}]}) {
    ...Fields ... on User @tag(value: "inline-runtime-secret") { id }
  }
}
fragment Fields on User @tag(value: "fragment-runtime-secret") { name }
mutation Other { update(input: {password: "other-operation-secret"}) { id } }
'''
    payload = {'query': source, 'operationName': 'Fetch', 'variables': {'input': {'password': 'variable-runtime-secret'}}}
    body = json.dumps(payload).encode()
    headers = {'X-Coverage-Run-Id': str(run.id), 'Authorization': 'Bearer authorization-secret', 'Cookie': 'e2e=cookie-runtime-secret'}
    response = await client.post('/graphql', content=body, headers=headers)
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and calls[0].content == body
    assert calls[0].headers['authorization'] == 'Bearer authorization-secret'
    row = await postgres_session.scalar(select(ObservedRequest).where(ObservedRequest.run_id == run.id))
    assert row.analysis_status == 'unsupported'  # AST display does not broaden Coverage Engine support.
    assert row.query_hash == hashlib.sha256(source.encode()).hexdigest()
    assert row.operation_key == operation_key(row.query_hash, 'Fetch', 'query')
    detail = await client.get(f'/api/runs/{run.id}/requests/{row.id}')
    assert detail.status_code == 200
    assert detail.json()['documentAvailability'] == 'redacted'
    safe = detail.json()['safeGraphqlDocument']
    assert 'fragment Fields on User' in safe and 'mutation Other' in safe and 'alias: user' in safe
    assert '$input' in safe and '@tag' in safe
    persisted = await postgres_session.scalar(text('SELECT to_jsonb(t)::text FROM observed_requests t WHERE id=:id'), {'id':row.id})
    rest_responses = [detail.text]
    for endpoint in ('requests','operations','stats'):
        result = await client.get(f'/api/runs/{run.id}/{endpoint}')
        assert result.status_code == 200
        rest_responses.append(result.text)
        if endpoint != 'requests':
            assert 'safeGraphqlDocument' not in result.text
        else:
            assert 'safeGraphqlDocument' not in result.json()[0]
    secrets = ('raw-comment-secret','default-runtime-secret','directive-runtime-secret','literal-runtime-secret',
               'nested-runtime-secret','987654','876.543','inline-runtime-secret','fragment-runtime-secret',
               'other-operation-secret','variable-runtime-secret','authorization-secret','cookie-runtime-secret','response-secret')
    for secret in secrets:
        assert secret not in persisted
        assert secret not in '\n'.join(rest_responses)
        assert secret not in caplog.text
    assert source not in persisted and source not in caplog.text
    assert (await get_run_stats(postgres_session, run.id)).counted_requests == 0


@pytest.mark.parametrize('mode', ['available','malformed','oversized','normalizer_failure','invalid_unicode'])
async def test_document_degradation_never_changes_capture_or_coverage(feature_context, postgres_session, monkeypatch, mode):
    client, service, snapshot, run = feature_context
    source = 'query Fetch($input: Credentials!) { user(input: $input) { id } }'
    if mode == 'malformed':
        source = 'query { user(input: "broken-secret) }'
    elif mode == 'invalid_unicode':
        source = 'query { user(input: "' + chr(0xD800) + '") { id } }'
    elif mode == 'oversized':
        source = '#'+('x'*262144)+'\n'+source
    elif mode == 'normalizer_failure':
        def fail(_):
            raise RuntimeError('normalizer-secret')
        monkeypatch.setattr(e2e_capture, 'normalize_document', fail)
    body = json.dumps({'query':source,'variables':{'input':{'password':'private-variable'}}}).encode()
    ident = await capture_e2e_exchange(postgres_session, run_id=run.id, request_body=body,
        decoded_response_body=b'{"data":{"user":{"id":"private-response"}}}', http_status=200,
        duration_ms=12, analyzer=analyze_e2e_query)
    row = await postgres_session.get(ObservedRequest, ident)
    assert row.query_hash == (None if mode == 'invalid_unicode' else hashlib.sha256(source.encode()).hexdigest())
    response = await client.get(f'/api/runs/{run.id}/requests/{ident}')
    assert response.status_code == 200
    data = response.json()
    assert data['documentAvailability'] == ('available' if mode == 'available' else 'unavailable')
    if mode != 'available':
        assert data['safeGraphqlDocument'] is None
    assert data['countedInCoverage'] == (mode not in ('malformed','invalid_unicode'))
    assert (await get_run_graph(postgres_session, run.id)).summary.covered_fields == (0 if mode in ('malformed','invalid_unicode') else 2)


async def test_schema_sdl_metadata_label_dedup_and_graph_identity(feature_context, postgres_session):
    client, service, snapshot, run = feature_context
    path = f'/api/schemas/{snapshot.id}'
    stored = await postgres_session.get(SchemaSnapshot, snapshot.id)
    before_sdl, before_hash = stored.schema_sdl, stored.schema_hash
    before_fields = (await postgres_session.scalars(select(SchemaField.id))).all()
    sdl = (await client.get(path+'/sdl')).json()
    assert sdl == {'schemaId': str(snapshot.id), 'schemaHash': before_hash, 'sdl': before_sdl}
    assert hashlib.sha256(sdl['sdl'].encode()).hexdigest() == before_hash
    graph = (await client.get(path+'/graph')).json()
    assert graph['rootTypes'] == {'query':'Read','mutation':'Write','subscription':'Events'}
    types = {t['name']:t for t in graph['types']}
    assert types['Read']['description'] == 'Root description' and types['Read']['rootOperations'] == ['query']
    assert types['Write']['rootOperations'] == ['mutation'] and types['Events']['rootOperations'] == ['subscription']
    assert types['User']['rootOperations'] == [] and types['User']['interfaces'] == ['Node']
    assert types['Node']['possibleTypes'] == ['Team','User']
    assert types['Search']['possibleTypes'] == ['Team','User'] and types['Search']['fields'] == []
    field = next(f for f in types['Read']['fields'] if f['name'] == 'user')
    assert field['description'] == 'Field description' and field['deprecationReason'] == 'Use node'
    arg, = field['args']
    assert arg['name'] == 'input' and arg['type'] == 'Credentials'
    assert arg['description'] == 'Input description'
    assert arg['defaultValueAvailability'] == 'redacted' and 'schema-default-secret' not in arg['defaultValue']
    assert 'schema-default-secret' not in json.dumps(graph)
    relations = graph['relations']
    assert len({r['id'] for r in relations}) == len(relations)
    assert any(r['toType']=='Search' for r in relations) and any(r['toType']=='Node' for r in relations)
    assert not any(r['fromType'] in ('Search','Node') and r['toType']=='User' for r in relations)
    coverage_before = await get_run_graph(postgres_session, run.id)
    changed = await client.patch(path, json={'displayName':' Release candidate '})
    assert changed.status_code == 200 and changed.json()['displayName'] == 'Release candidate'
    assert changed.json()['id'] == str(snapshot.id) and changed.json()['schemaHash'] == before_hash
    assert await import_sdl(postgres_session, service.id, SDL) == type(snapshot)(**changed.json())
    assert (await client.get('/api/schemas', params={'serviceId':str(service.id)})).json()[0]['displayName'] == 'Release candidate'
    assert (await client.get(path+'/sdl')).json() == sdl
    assert (await client.get(path+'/graph')).json() == graph
    assert await get_run_graph(postgres_session, run.id) == coverage_before
    assert (await postgres_session.scalars(select(SchemaField.id))).all() == before_fields
    assert (await client.patch(path, json={'displayName':None})).json()['displayName'] is None
    for body in ({}, {'displayName':' '}, {'displayName':'x'*256}, {'displayName':'safe','schemaHash':'forged'}):
        assert (await client.patch(path, json=body)).status_code == 422
    assert (await client.get(f'/api/schemas/{uuid.uuid4()}/sdl')).status_code == 404
    assert (await client.get('/api/schemas/bad/sdl')).status_code == 422


@pytest.mark.parametrize('role', [None,'viewer','analyst','operator','admin','cli'])
async def test_document_sdl_metadata_label_roles_and_revocation(auth_context, postgres_session, role):
    client, users, private, public, private_schema, public_schema, private_run, public_run = auth_context
    ids=[]
    for run, name in ((private_run,'privateField'),(public_run,'publicField')):
        ids.append(await capture_e2e_exchange(postgres_session, run_id=run.id,
            request_body=json.dumps({'query':'{ '+name+' }'}).encode(), decoded_response_body=b'{"data":{}}',
            http_status=200, duration_ms=1, analyzer=analyze_e2e_query))
    headers={'X-Coverage-Api-Key':TEST_KEY} if role=='cli' else {}
    write_headers=headers
    if role not in (None,'cli'):
        write_headers,_=await login(client,role)
    privileged=role in ('admin','cli')
    for service,snapshot,run,ident in zip((private,public),(private_schema,public_schema),(private_run,public_run),ids):
        for suffix in ('sdl','graph'):
            response=await client.get(f'/api/schemas/{snapshot.id}/{suffix}',headers=headers)
            assert response.status_code==(200 if privileged or service.is_public else 404)
        detail=await client.get(f'/api/runs/{run.id}/requests/{ident}',headers=headers)
        assert detail.status_code==(200 if privileged else 401 if role is None else 403 if role=='viewer' else 404)
        if detail.status_code==200:
            assert detail.json()['documentAvailability']=='available'
            assert detail.json()['safeGraphqlDocument']
        changed=await client.patch(f'/api/schemas/{snapshot.id}',json={'displayName':'label'},headers=write_headers)
        assert changed.status_code==(200 if privileged else 401 if role is None else 404 if role=='operator' else 403)
    if role not in (None,'admin','cli'):
        for service in (private,public):
            await grant(postgres_session,users[role],service)
        for snapshot,run,ident in zip((private_schema,public_schema),(private_run,public_run),ids):
            assert (await client.get(f'/api/schemas/{snapshot.id}/sdl')).status_code==200
            detail=await client.get(f'/api/runs/{run.id}/requests/{ident}')
            assert detail.status_code==(403 if role=='viewer' else 200)
            if role=='operator':
                assert (await client.patch(f'/api/schemas/{snapshot.id}',json={'displayName':'with grant'},headers={})).status_code==403
                assert (await client.patch(f'/api/schemas/{snapshot.id}',json={'displayName':'with grant'},headers=write_headers)).status_code==200
        await postgres_session.execute(delete(ServiceGrant).where(ServiceGrant.user_id==users[role].id))
        await postgres_session.commit()
        if role!='viewer':
            for run,ident in zip((private_run,public_run),ids):
                assert (await client.get(f'/api/runs/{run.id}/requests/{ident}')).status_code==404
        assert (await client.get(f'/api/schemas/{private_schema.id}/sdl')).status_code==404
    row=await postgres_session.get(GraphQLService,public.id)
    row.is_public=False
    await postgres_session.commit()
    assert (await client.get(f'/api/schemas/{public_schema.id}/sdl',headers=headers)).status_code==(200 if privileged else 404)


async def test_historical_migration_and_safe_document_constraints(isolated_database):
    async with isolated_database() as session:
        service=await create_service(session,'historic-final','Historical')
        snapshot=await import_sdl(session,service.id,'type Query { field: String }')
        run=await create_run(session,snapshot.id,'Historical',{})
        url=session.bind.url.render_as_string(hide_password=False)
    async def migrate(*args):
        result=await asyncio.to_thread(subprocess.run,[str(Path(sys.executable).with_name('alembic')),*args],
            env={**os.environ,'DATABASE_URL':url},capture_output=True,text=True)
        assert result.returncode==0,result.stdout+result.stderr
    await migrate('downgrade','20261117_history_journal')
    ident=uuid.uuid4()
    async with isolated_database() as session:
        await session.execute(text('''INSERT INTO observed_requests(id,run_id,query_hash,operation_name,operation_type,operation_key,http_method,analysis_status,duration_ms)
            VALUES (:id,:run,:hash,'Legacy','query',:key,'POST','success',42)'''),
            {'id':ident,'run':run.id,'hash':'a'*64,'key':operation_key('a'*64,'Legacy','query')})
        before=(await session.execute(text('SELECT to_jsonb(t) FROM observed_requests t'))).scalar()
        await session.commit()
    await migrate('upgrade','head')
    async with isolated_database() as session:
        after=(await session.execute(text('SELECT to_jsonb(t) FROM observed_requests t'))).scalar()
        assert {k:v for k,v in after.items() if k not in ('safe_graphql_document','document_availability',
            'graphql_error_count','graphql_error_details','graphql_error_details_availability','field_evidence_version',
            'max_depth')}==before
        assert after['safe_graphql_document'] is None and after['document_availability']=='unavailable'
        assert after['graphql_error_count'] is None and after['graphql_error_details'] is None
        assert after['graphql_error_details_availability'] == 'unavailable'
        assert after['field_evidence_version'] is None
        assert after['max_depth'] is None
        assert (await session.get(SchemaSnapshot,snapshot.id)).display_name is None
        for document,status in ((None,'available'),('{ field }','unavailable'),('x'*65537,'redacted'),('', 'available'),(None,'unknown')):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(text('UPDATE observed_requests SET safe_graphql_document=:doc, document_availability=:status WHERE id=:id'),
                        {'doc':document,'status':status,'id':ident})
        row=await session.get(ObservedRequest,ident)
        assert row.query_hash=='a'*64 and row.operation_key==operation_key('a'*64,'Legacy','query')
    await migrate('check')


async def test_normalization_failure_after_mutation_does_not_repeat_upstream(feature_context, postgres_session, monkeypatch):
    client, _, _, run = feature_context
    calls=[]
    async def upstream(self,request):
        calls.append(request)
        return raw_response(200,b'{"data":{"update":{"id":"mutation-response-secret"}}}')
    def fail(_):
        raise RuntimeError('private-normalizer-error')
    monkeypatch.setattr(PinnedTransport,'handle_async_request',upstream)
    monkeypatch.setattr(e2e_capture,'normalize_document',fail)
    source='mutation Save { update(input: {password: "mutation-input-secret"}) { id } }'
    response=await client.post('/graphql',json={'query':source},headers={'X-Coverage-Run-Id':str(run.id)})
    assert response.status_code==200 and len(calls)==1
    assert json.loads(calls[0].content)['query']==source
    row=await postgres_session.scalar(select(ObservedRequest).where(ObservedRequest.run_id==run.id))
    assert row.analysis_status=='success' and row.safe_graphql_document is None
    assert row.document_availability=='unavailable'
    assert row.operation_key==operation_key(hashlib.sha256(source.encode()).hexdigest(),'Save','mutation')
    assert (await get_run_stats(postgres_session,run.id)).counted_requests==1


async def test_invalid_sdl_errors_are_generic_and_leave_no_partial_snapshot(feature_context, postgres_session):
    client,service,snapshot,run=feature_context
    for source in ('type Query { field(arg: String = "private-schema-secret) : Int }',
                   'type Query { '+('f'*256)+': String }'):
        response=await client.post('/api/schemas/import',json={'serviceId':str(service.id),'sdl':source})
        assert response.status_code==422
        assert 'private-schema-secret' not in response.text
        assert await postgres_session.scalar(select(func.count()).select_from(SchemaSnapshot)
            .where(SchemaSnapshot.service_id==service.id))==1


async def test_historical_root_assignments_are_preserved_on_read(feature_context, postgres_session):
    # graphql-core 3.2 accepted shared roots; 3.3 validates new imports more
    # strictly. Reading a saved historical snapshot must preserve its metadata.
    from graphql import build_schema, print_schema
    client,service,_,_=feature_context
    sdl=print_schema(build_schema('schema { query: Root mutation: Root subscription: Root } type Root { field: String }'))
    snapshot=SchemaSnapshot(service_id=service.id,schema_sdl=sdl,schema_hash=hashlib.sha256(sdl.encode()).hexdigest(),source='sdl')
    postgres_session.add(snapshot)
    await postgres_session.flush()
    root_type=SchemaType(snapshot_id=snapshot.id,name='Root',kind='OBJECT')
    postgres_session.add(root_type)
    await postgres_session.flush()
    postgres_session.add(SchemaField(type_id=root_type.id,name='field',return_type='String'))
    await postgres_session.commit()
    response=await client.get(f'/api/schemas/{snapshot.id}/graph')
    assert response.status_code==200
    assert response.json()['rootTypes']=={'query':'Root','mutation':'Root','subscription':'Root'}
    root,=response.json()['types']
    assert root['name']=='Root' and root['kind']=='OBJECT'
    assert root['rootOperations']==['query','mutation','subscription']
    assert response.json()['summary']['totalFields']==1
