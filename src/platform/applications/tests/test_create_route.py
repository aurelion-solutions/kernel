# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from httpx import ASGITransport, AsyncClient
import pytest


@pytest.mark.asyncio
async def test_create_application_returns_201_and_created_application(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {'queue': 'test-queue'},
                'required_connector_tags': ['jira', 'eu-segment'],
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['name'] == 'my-app'
    assert data['code'] == 'my-app'
    assert data['config'] == {'queue': 'test-queue'}
    assert data['required_connector_tags'] == ['jira', 'eu-segment']
    assert data['is_active'] is True
    assert 'created_at' in data
    assert 'updated_at' in data


@pytest.mark.asyncio
async def test_create_application_missing_name_returns_422(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/applications',
            json={
                'code': 'my-app',
                'config': {},
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_application_empty_name_returns_422(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/applications',
            json={
                'name': '',
                'code': 'my-app',
                'config': {},
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_application_defaults_required_connector_tags_to_empty_list(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/applications',
            json={
                'name': 'empty-tags-app',
                'code': 'empty-tags-app',
                'config': {},
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert data['required_connector_tags'] == []


@pytest.mark.asyncio
async def test_create_application_201_with_code(app):
    """POST with code returns 201 and response contains code."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/applications',
            json={'name': 'AD Prod', 'code': 'ad'},
        )
    assert response.status_code == 201
    data = response.json()
    assert data['code'] == 'ad'


@pytest.mark.asyncio
async def test_create_application_missing_code_returns_422(app):
    """POST without code returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/applications',
            json={'name': 'AD Prod'},
        )
    assert response.status_code == 422


@pytest.mark.parametrize(
    'code',
    [
        'Active Directory',  # spaces + uppercase
        'AD',  # uppercase
        ' ad',  # leading space
        '-ad',  # leading dash
        'ad!',  # invalid char
        'a' * 65,  # too long
        '',  # empty
    ],
)
@pytest.mark.asyncio
async def test_create_application_invalid_code_returns_422(app, code):
    """POST with invalid code returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/applications',
            json={'name': 'Test', 'code': code},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_application_duplicate_code_returns_409(app):
    """POST code='ad' twice returns 409 on second request."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        r1 = await client.post(
            '/api/v0/applications',
            json={'name': 'AD Prod', 'code': 'ad'},
        )
        assert r1.status_code == 201
        r2 = await client.post(
            '/api/v0/applications',
            json={'name': 'AD Stage', 'code': 'ad'},
        )
    assert r2.status_code == 409
