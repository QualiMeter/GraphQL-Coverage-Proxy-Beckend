"""Invalid SDL is rejected before persistence; browser ACL/CSRF still gate parsing."""
import uuid

import httpx
import pytest

from app.main import app
from sqlalchemy import delete, func, select, update

from app import services
from app.auth_models import ServiceGrant, User
from app.models import SchemaField, SchemaSnapshot, SchemaType
from test_service_dns_transactions_postgres import service_dns_app  # noqa: F401
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration


async def counts(ctx):
    async with ctx.sessions() as session:
        return tuple([await session.scalar(select(func.count()).select_from(model))
                      for model in (SchemaSnapshot, SchemaType, SchemaField)])


@pytest.mark.parametrize('sdl', [
    'type Query { user: UnknownType }',
    'type Query { user: String @unknownDirective }',
    'type Query { user: String user: String }',
])
async def test_invalid_sdl_returns_generic_422_without_schema_rows(service_dns_app, sdl, caplog):
    ctx = service_dns_app
    before = await counts(ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=ctx.client.base_url, cookies=ctx.client.cookies) as client:
        response = await client.post('/api/schemas/import',
            json={'serviceId': str(ctx.service.id), 'sdl': sdl}, headers=ctx.cookie_headers)
    assert response.status_code == 422
    assert response.json() == {'detail': 'Invalid or unsupported GraphQL schema'}
    assert await counts(ctx) == before
    assert ctx.engine.pool.checkedout() == 0
    assert not caplog.records  # Expected input errors need no internal-failure event.
    for private in ('UnknownType', 'unknownDirective', sdl):
        assert private not in response.text + caplog.text


@pytest.mark.parametrize('denial,status', [
    ('anonymous', 401), ('csrf', 403), ('origin', 403), ('grant', 404), ('role', 403), ('service', 404),
])
async def test_invalid_sdl_cannot_bypass_access_checks(denial, status, service_dns_app, monkeypatch):
    ctx = service_dns_app
    before = await counts(ctx)
    headers = dict(ctx.cookie_headers)
    service_id = ctx.service.id
    if denial == 'anonymous':
        ctx.client.cookies.clear()
        headers = {}
    elif denial == 'csrf':
        headers.pop('X-CSRF-Token')
    elif denial == 'origin':
        headers['Origin'] = 'https://attacker.invalid'
    elif denial == 'grant':
        async with ctx.sessions() as session:
            await session.execute(delete(ServiceGrant).where(ServiceGrant.user_id == ctx.user_id))
            await session.commit()
    elif denial == 'role':
        async with ctx.sessions() as session:
            await session.execute(update(User).where(User.id == ctx.user_id).values(role='viewer'))
            await session.commit()
    else:
        service_id = uuid.uuid4()
    def forbidden(*args):
        pytest.fail('Unauthorized/missing service must be rejected before parsing')
    monkeypatch.setattr(services, 'parse_sdl', forbidden)
    response = await ctx.client.post('/api/schemas/import',
        json={'serviceId': str(service_id), 'sdl': 'type Query { user: UnknownType }'}, headers=headers)
    assert response.status_code == status, response.text
    assert await counts(ctx) == before
    assert ctx.engine.pool.checkedout() == 0


async def test_storage_typeerror_is_not_misclassified_as_invalid_sdl(service_dns_app, monkeypatch):
    ctx = service_dns_app
    before = await counts(ctx)
    async def broken(*args, **kwargs):
        raise TypeError('unexpected storage bug')
    monkeypatch.setattr(services, '_save_parsed_schema', broken)
    with pytest.raises(TypeError, match='unexpected storage bug'):
        await ctx.client.post('/api/schemas/import',
            json={'serviceId': str(ctx.service.id), 'sdl': 'type Query { x: String }'}, headers=ctx.cookie_headers)
    assert await counts(ctx) == before
    assert ctx.engine.pool.checkedout() == 0
