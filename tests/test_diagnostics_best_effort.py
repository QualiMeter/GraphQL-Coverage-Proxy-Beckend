"""Diagnostics failures must not replace the response to an executed mutation."""
import asyncio
import logging
import sys
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app import diagnostics, e2e_capture, proxy
from app.main import app
from test_error_diagnostics import BODY, WIRE, SECRETS, storage_failure, wire_headers
from test_graphql_proxy import client, raw_response, upstream  # noqa: F401
from test_proxy_e2e import capture_context  # noqa: F401

WARNING = {'detail': 'Coverage observation could not be saved; upstream may have executed the request. Do not retry automatically.'}


class BrokenHandler(logging.Handler):
    def emit(self, record):
        raise OSError('logging output unavailable')


@pytest.fixture
def attached_handler():
    attached = []
    def attach(handler):
        diagnostics.logger.addHandler(handler)
        attached.append(handler)
        return handler
    yield attach
    for handler in attached:
        diagnostics.logger.removeHandler(handler)


def private_output_is_absent(response, caplog, capfd):
    # The owned stderr buffer writes to fd 2: capture the actual descriptor.
    output = caplog.text + capfd.readouterr().err
    # Successful proxy responses intentionally retain upstream bodies verbatim.
    # Error responses must additionally be free of every canary.
    if response.status_code >= 400:
        output += response.text
    for value in (*SECRETS, 'INSERT secret SQL', 'parameters:', 'Logging error'):
        assert value not in output


async def test_b1_handler_oserror_preserves_http_502_and_no_retry(
    client, upstream, capture_context, attached_handler, caplog, capfd,
):
    attached_handler(BrokenHandler())
    capture_context.capture.side_effect = storage_failure()
    requests = upstream(lambda request: raw_response(200, WIRE))
    # Render unhandled errors as real HTTP 500, rather than just raising in ASGI.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=client.base_url, headers=client.headers) as http:
        result = await http.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == 502
    assert result.json() == WARNING
    assert len(requests) == 1
    private_output_is_absent(result, caplog, capfd)


class BrokenSQLState(Exception):
    @property
    def sqlstate(self):
        raise RuntimeError(' '.join(SECRETS))


async def test_bad_sqlstate_accessor_preserves_http_502(
    client, upstream, capture_context, caplog, capfd,
):
    capture_context.capture.side_effect = DBAPIError('INSERT secret SQL',
        {'password': 'password-secret'}, BrokenSQLState(' '.join(SECRETS)))
    requests = upstream(lambda request: raw_response(200, WIRE))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=client.base_url, headers=client.headers) as http:
        result = await http.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == 502 and result.json() == WARNING
    assert len(requests) == 1
    records = [r.diagnostic for r in caplog.records if hasattr(r, 'diagnostic')]
    assert records == [{'event': 'coverage_internal_failure', 'stage': 'observation_save',
                       'exception_type': 'DBAPIError', 'run_id': str(capture_context.run.id)}]
    private_output_is_absent(result, caplog, capfd)


@pytest.mark.parametrize('fault', ['formatter', 'stream', 'stderr'])
async def test_handleerror_never_prints_original_exception_context(
    fault, client, upstream, capture_context, attached_handler, monkeypatch, caplog, capfd,
):
    monkeypatch.setattr(logging, 'raiseExceptions', True)
    target = next((h for h in diagnostics.logger.handlers if isinstance(h, logging.StreamHandler)
                   and not hasattr(h, 'records')), None)
    if target is None:  # Original HEAD uses the ordinary propagating logging path.
        target = attached_handler(logging.StreamHandler(sys.stderr))
    if fault == 'formatter':
        class BrokenFormatter(logging.Formatter):
            def format(self, record):
                raise OSError('formatter unavailable')
        monkeypatch.setattr(target, 'formatter', BrokenFormatter())
    else:
        real_stderr = sys.stderr
        class IntermittentStderr:
            failed = False
            def write(self, message):
                if fault == 'stream' and not self.failed:
                    self.failed = True
                    raise OSError('stream unavailable once')
                return real_stderr.write(message)
            def flush(self):
                if fault == 'stderr' and not self.failed:
                    self.failed = True
                    raise OSError('stderr flush unavailable once')
                return real_stderr.flush()
        stream = IntermittentStderr()
        monkeypatch.setattr(sys, 'stderr', stream)
        monkeypatch.setattr(target, 'stream', stream)
    capture_context.capture.side_effect = storage_failure()
    requests = upstream(lambda request: raw_response(200, WIRE))
    result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == 502 and result.json() == WARNING
    assert len(requests) == 1
    private_output_is_absent(result, caplog, capfd)


@pytest.mark.parametrize('point', ['preflight', 'timeout', 'sdl_storage', 'introspection_storage', 'analyzer', 'document'])
async def test_failed_diagnostic_sink_preserves_other_public_contracts(
    point, client, upstream, capture_context, attached_handler, monkeypatch, caplog, capfd,
):
    from app import api
    attached_handler(BrokenHandler())
    save = AsyncMock(return_value=uuid.uuid4())
    if point == 'preflight':
        capture_context.session.get.side_effect = storage_failure()
    elif point in {'analyzer', 'document'}:
        monkeypatch.setattr(proxy, 'capture_e2e_exchange', e2e_capture.capture_e2e_exchange)
        monkeypatch.setattr(e2e_capture, 'record_observation', save)
        if point == 'analyzer':
            capture_context.analyzer.side_effect = RuntimeError(' '.join(SECRETS))
        else:
            def broken(*args):
                raise RuntimeError(' '.join(SECRETS))
            monkeypatch.setattr(e2e_capture, 'normalize_document', broken)
    elif point.endswith('storage'):
        handler = 'import_sdl_schema' if point == 'sdl_storage' else 'import_introspection_schema'
        monkeypatch.setattr(api, handler, AsyncMock(side_effect=storage_failure()))
    def respond(request):
        if point == 'timeout':
            raise httpx.ReadTimeout(' '.join(SECRETS), request=request)
        return raw_response(202, WIRE)
    requests = upstream(respond)
    if point.endswith('storage'):
        path = '/api/schemas/import' + ('/introspection' if point == 'introspection_storage' else '')
        payload = {'serviceId': str(uuid.uuid4())}
        if point == 'sdl_storage':
            payload['sdl'] = 'type Query { x: String }'
        result = await client.post(path, json=payload)
        expected = ({'detail': 'Schema storage is unavailable'} if point == 'sdl_storage' else
                    {'detail': 'Schema storage is temporarily unavailable', 'code': 'SCHEMA_STORAGE_UNAVAILABLE'})
        assert result.status_code == 503 and result.json() == expected
        assert requests == []
    else:
        result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
        if point == 'preflight':
            assert result.status_code == 503 and result.json() == {'detail': 'Coverage storage is unavailable'}
            assert requests == []
        elif point == 'timeout':
            assert result.status_code == 504
            assert result.json() == {'detail': 'GraphQL upstream timed out; upstream may have executed the request. Do not retry automatically.'}
            assert len(requests) == 1
        else:
            assert result.status_code == 202 and result.content == WIRE
            assert len(requests) == 1
            observation = save.await_args.args[1]
            assert observation.analysis.status == ('internal_error' if point == 'analyzer' else 'success')
            if point == 'document':
                assert observation.safe_document.text is None
    private_output_is_absent(result, caplog, capfd)


async def test_enqueue_failure_is_best_effort_at_http_boundary(
    client, upstream, capture_context, monkeypatch, caplog, capfd,
):
    def broken(*args):
        raise OSError('queue unavailable')
    monkeypatch.setattr(diagnostics._pending, 'put_nowait', broken)
    capture_context.capture.side_effect = storage_failure()
    requests = upstream(lambda request: raw_response(200, WIRE))
    result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == 502 and result.json() == WARNING
    assert len(requests) == 1
    private_output_is_absent(result, caplog, capfd)


async def test_blocked_sink_and_full_queue_never_hold_event_loop_or_retry(
    client, upstream, capture_context, attached_handler, caplog, capfd,
):
    from conftest import drain_diagnostics
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    class BlockedHandler(logging.Handler):
        def emit(self, record):
            assert threading.get_ident() != loop_thread, 'handler I/O ran on event loop'
            entered.set()
            assert release.wait(15)
    attached_handler(BlockedHandler())
    capture_context.capture.side_effect = storage_failure()
    requests = upstream(lambda request: raw_response(200, WIRE))
    async def post():
        result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
        assert result.status_code == 502 and result.json() == WARNING
        return result
    try:
        await post()
        assert await asyncio.to_thread(entered.wait, 5)
        serviced = asyncio.Event()
        async def independent_coroutine():
            await asyncio.sleep(0)
            serviced.set()
        count = diagnostics._pending.maxsize + 8
        results = await asyncio.wait_for(asyncio.gather(
            independent_coroutine(), *[post() for _ in range(count)]), timeout=10)
        assert serviced.is_set() and not release.is_set()
        assert diagnostics._pending.qsize() == diagnostics._pending.maxsize
        assert len(requests) == count + 1  # Dropped diagnostics never retry HTTP.
    finally:
        release.set()
        await asyncio.to_thread(drain_diagnostics)
    private_output_is_absent(results[1], caplog, capfd)


async def test_concurrent_http_failures_keep_context_and_cancellation_is_not_logged(
    client, upstream, capture_context, monkeypatch, caplog, capfd,
):
    from app.models import TestRun as Run
    monkeypatch.setattr(proxy, 'capture_e2e_exchange', e2e_capture.capture_e2e_exchange)
    save = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(e2e_capture, 'record_observation', save)
    original_get = capture_context.session.get.side_effect
    async def get(model, key, **kwargs):
        if model is Run:
            return SimpleNamespace(id=key, snapshot_id=capture_context.snapshot.id, status='running')
        return await original_get(model, key, **kwargs)
    capture_context.session.get.side_effect = get
    ready, release = asyncio.Event(), asyncio.Event()
    run_ids = [uuid.uuid4() for _ in range(12)]
    entered = []
    async def analyzer(gql, sdl):
        assert diagnostics._run_id.get() == uuid.UUID(gql.variables['run'])
        if gql.variables.get('gated'):
            entered.append(gql.variables['run'])
            if len(entered) == len(run_ids):
                ready.set()
            await release.wait()
        raise RuntimeError(' '.join(SECRETS))
    app.state.coverage_analyzer = analyzer
    requests = upstream(lambda request: raw_response(202, WIRE))
    async def post(run_id, gated=True):
        try:
            result = await client.post('/graphql', json={**BODY,
                'variables': {'run': str(run_id), 'gated': gated, 'password': 'variable-secret'}},
                headers=wire_headers(run_id))
            assert result.status_code == 202 and result.content == WIRE
            return result
        finally:
            assert diagnostics._run_id.get() is None
    tasks = [asyncio.create_task(post(run_id)) for run_id in run_ids]
    try:
        await asyncio.wait_for(ready.wait(), 10)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        results = await asyncio.gather(*tasks[1:])
        fresh_id = uuid.uuid4()
        await post(fresh_id, gated=False)
        records = [r.diagnostic for r in caplog.records if hasattr(r, 'diagnostic')]
        assert {r['run_id'] for r in records} == {str(r) for r in run_ids[1:]} | {str(fresh_id)}
        assert len(records) == len(run_ids)
        assert all(r['stage'] == 'analyzer' for r in records)
        assert save.await_count == len(run_ids)
        assert len(requests) == len(run_ids) + 1
        assert diagnostics._run_id.get() is None
        private_output_is_absent(results[0], caplog, capfd)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_isolated_diagnostics_are_visible_with_real_uvicorn_configuration():
    import os
    from pathlib import Path
    import subprocess
    import json
    program = '''
import uuid
from app.server import server_config
config = server_config()
config.load()
from app.diagnostics import internal_failure, _pending
from sqlalchemy.exc import DBAPIError
config.configure_logging()
try:
    raise DBAPIError("INSERT secret SQL", {"password": "password-secret"}, RuntimeError("token-secret"))
except Exception as exc:
    internal_failure("observation_save", exc, run_id=uuid.UUID(int=1))
_pending.join()
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, 'FORWARDED_ALLOW_IPS': '127.0.0.1'}, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{')]
    assert events == [{'event': 'coverage_internal_failure', 'stage': 'observation_save',
        'exception_type': 'DBAPIError', 'run_id': str(uuid.UUID(int=1))}]
    for value in (*SECRETS, 'INSERT secret SQL', 'parameters:', 'Logging error'):
        assert value not in result.stdout + result.stderr
