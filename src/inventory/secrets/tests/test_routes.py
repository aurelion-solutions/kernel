# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for secret routes."""

from pathlib import Path

from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.secrets.deps import get_secret_service
from src.inventory.secrets.models import Secret
from src.inventory.secrets.service import SecretService
from src.platform.secrets.factory import SecretManagerFactory
from src.platform.secrets.providers.file import FileSecretManager


@pytest.fixture
def app_with_file_provider(app, tmp_path: Path):
    """App with secret service using file provider at tmp_path."""
    factory = SecretManagerFactory()
    factory.register('file', lambda: FileSecretManager(path=tmp_path / 'secrets.json'))
    service = SecretService(factory=factory)
    app.dependency_overrides[get_secret_service] = lambda: service
    return app


@pytest.mark.asyncio
async def test_create_secret_via_api(app_with_file_provider):
    """POST /secrets creates secret and returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_file_provider),
        base_url='http://testserver',
    ) as c:
        response = await c.post(
            '/api/v0/secrets',
            json={
                'key': 'test/key',
                'provider': 'file',
                'namespace': 'default',
                'value': 'secret_value',
            },
        )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_retrieve_secret_via_api(app_with_file_provider):
    """GET /secrets/{provider}/{key} returns 200 and secret value."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_file_provider),
        base_url='http://testserver',
    ) as c:
        await c.post(
            '/api/v0/secrets',
            json={'key': 'app/token', 'provider': 'file', 'namespace': 'ns', 'value': 'retrieved_value'},
        )
        response = await c.get('/api/v0/secrets/file/app/token?namespace=ns')
    assert response.status_code == 200
    assert response.text == 'retrieved_value'


@pytest.mark.asyncio
async def test_delete_secret_via_api(app_with_file_provider):
    """DELETE /secrets/{provider}/{key} returns 204."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_file_provider),
        base_url='http://testserver',
    ) as c:
        await c.post(
            '/api/v0/secrets',
            json={'key': 'to_delete', 'provider': 'file', 'namespace': 'default', 'value': 'x'},
        )
        response = await c.delete('/api/v0/secrets/file/to_delete?namespace=default')
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_overwrite_secret(app_with_file_provider):
    """POST same key twice, GET returns new value."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_file_provider),
        base_url='http://testserver',
    ) as c:
        await c.post(
            '/api/v0/secrets',
            json={'key': 'overwrite/me', 'provider': 'file', 'namespace': 'default', 'value': 'first'},
        )
        await c.post(
            '/api/v0/secrets',
            json={'key': 'overwrite/me', 'provider': 'file', 'namespace': 'default', 'value': 'second'},
        )
        response = await c.get('/api/v0/secrets/file/overwrite/me?namespace=default')
    assert response.status_code == 200
    assert response.text == 'second'


@pytest.mark.asyncio
async def test_unknown_provider_error(app_with_file_provider):
    """GET/DELETE with bad provider returns 400."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_file_provider),
        base_url='http://testserver',
    ) as c:
        get_resp = await c.get('/api/v0/secrets/unknown/key?namespace=default')
        del_resp = await c.delete('/api/v0/secrets/unknown/key?namespace=default')
    assert get_resp.status_code == 400
    assert del_resp.status_code == 400
    assert 'Unsupported' in get_resp.json()['detail']
    assert 'Unsupported' in del_resp.json()['detail']


@pytest.mark.asyncio
async def test_missing_key_error(app_with_file_provider):
    """GET/DELETE non-existent key returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_file_provider),
        base_url='http://testserver',
    ) as c:
        get_resp = await c.get('/api/v0/secrets/file/nonexistent/key?namespace=default')
        del_resp = await c.delete('/api/v0/secrets/file/nonexistent/key?namespace=default')
    assert get_resp.status_code == 404
    assert del_resp.status_code == 404
    assert 'Secret not found' in get_resp.json()['detail']
    assert 'Secret not found' in del_resp.json()['detail']


@pytest.mark.asyncio
async def test_list_secrets_returns_200_and_array(session_factory, app):
    """Successful GET /secrets returns 200 and list of secret metadata."""
    async with session_factory() as session:
        s1 = Secret(key='github/token', provider='file', namespace='default')
        s2 = Secret(key='db/password', provider='file', namespace='prod')
        session.add(s1)
        session.add(s2)
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/secrets')

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2
    keys = [s['key'] for s in data]
    assert 'github/token' in keys
    assert 'db/password' in keys
    for item in data:
        assert 'key' in item
        assert 'provider' in item
        assert 'namespace' in item
        assert 'value' not in item


@pytest.mark.asyncio
async def test_list_secrets_returns_empty_array_when_none(app):
    """GET /secrets returns empty array when no secrets exist."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/secrets')

    assert response.status_code == 200
    assert response.json() == []
