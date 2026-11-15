"""A real PostgreSQL write rejection after upstream execution remains a safe 502."""
import json

import pytest
from sqlalchemy import func, select
from starlette.datastructures import State

from app.main import app
from app.models import ObservedRequest
from app.analyzer_adapter import analyze_e2e_query
from test_graphql_proxy import raw_response, upstream  # noqa: F401
from test_proxy_e2e_postgres import http_client, make_run  # noqa: F401
from test_e2e_capture_postgres import postgres_session  # noqa: F401

pytestmark = pytest.mark.integration


async def test_real_observation_constraint_failure_is_diagnosed_and_not_retried(
    postgres_session, http_client, upstream, monkeypatch, caplog,
):
    run, _, _ = await make_run(postgres_session)
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    # HTTPX accepts a nonstandard upstream status, but the real PostgreSQL
    # observation CHECK deliberately rejects values outside 100..599.
    requests = upstream(lambda request: raw_response(700, b'{"data":{"user":{"id":"response-secret"}}}'))
    query = 'query Fetch { user { id } } # request-secret'
    response = await http_client.post('/graphql', json={
        'query': query, 'variables': {'password': 'variable-secret'},
    }, headers={'X-Coverage-Run-Id': str(run.id), 'Authorization': 'Bearer token-secret',
                'Cookie': 'session=cookie-secret'})
    assert response.status_code == 502
    assert response.json() == {'detail': 'Coverage observation could not be saved; upstream may have executed the request. Do not retry automatically.'}
    assert len(requests) == 1
    await postgres_session.rollback()
    assert await postgres_session.scalar(select(func.count()).select_from(ObservedRequest).where(
        ObservedRequest.run_id == run.id)) == 0
    records = [record for record in caplog.records if hasattr(record, 'diagnostic')]
    record, = records
    assert record.diagnostic == {'event': 'coverage_internal_failure', 'stage': 'observation_save',
        'exception_type': 'IntegrityError', 'sqlstate': '23514', 'run_id': str(run.id)}
    assert json.loads(record.getMessage()) == record.diagnostic
    assert record.exc_info is None and record.stack_info is None
    for secret in (query, 'request-secret', 'response-secret', 'variable-secret', 'token-secret', 'cookie-secret',
                   'INSERT INTO', 'parameters:'):
        assert secret not in caplog.text + response.text
