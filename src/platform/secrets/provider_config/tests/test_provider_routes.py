# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for provider API routes."""

import pytest


@pytest.mark.asyncio
async def test_list_providers(client) -> None:
    """GET /secrets/providers returns list of providers."""
    response = await client.get('/api/v0/secrets/providers')
    assert response.status_code == 200
    providers = response.json()
    assert isinstance(providers, list)
    names = [p['name'] for p in providers]
    assert 'file' in names
    assert 'vault' in names


@pytest.mark.asyncio
async def test_create_and_delete_provider(client) -> None:
    """Create custom provider, list it, delete it."""
    # Create
    r = await client.post(
        '/api/v0/secrets/providers',
        json={'name': 'myfile', 'type': 'file', 'config': {'path': '/tmp/test-secrets.json'}},
    )
    assert r.status_code == 201
    assert r.json()['name'] == 'myfile'

    # List includes it
    r = await client.get('/api/v0/secrets/providers')
    names = [p['name'] for p in r.json()]
    assert 'myfile' in names

    # Delete
    r = await client.delete('/api/v0/secrets/providers/myfile')
    assert r.status_code == 204

    # Gone from list
    r = await client.get('/api/v0/secrets/providers')
    names = [p['name'] for p in r.json()]
    assert 'myfile' not in names


@pytest.mark.asyncio
async def test_create_provider_conflicts_with_builtin(client) -> None:
    """Cannot create provider with built-in name."""
    r = await client.post(
        '/api/v0/secrets/providers',
        json={'name': 'file', 'type': 'file', 'config': {}},
    )
    assert r.status_code == 400
