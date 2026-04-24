# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /capability-scope-keys routes."""

from __future__ import annotations

import pytest

_BASE = '/api/v0/capability-scope-keys'


@pytest.mark.asyncio
async def test_post_scope_key_returns_201(client) -> None:
    """POST /capability-scope-keys with valid payload returns 201 and CapabilityScopeKeyRead body."""
    response = await client.post(
        _BASE,
        json={
            'code': 'GLOBAL',
            'name': 'Global',
            'description': 'Platform-wide scope.',
            'is_active': True,
            'created_by': 'alice@example.com',
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body['code'] == 'GLOBAL'
    assert body['name'] == 'Global'
    assert body['description'] == 'Platform-wide scope.'
    assert body['is_active'] is True
    assert body['created_by'] == 'alice@example.com'
    assert 'id' in body
    assert body['id'] > 0
    assert 'created_at' in body


@pytest.mark.asyncio
async def test_post_scope_key_duplicate_code_returns_409(client) -> None:
    """POST /capability-scope-keys with a duplicate code returns 409."""
    payload = {'code': 'LEGAL_ENTITY', 'name': 'Legal entity'}
    first = await client.post(_BASE, json=payload)
    assert first.status_code == 201

    second = await client.post(_BASE, json={**payload, 'name': 'Legal entity dup'})
    assert second.status_code == 409
    assert 'LEGAL_ENTITY' in second.json()['detail']


@pytest.mark.asyncio
async def test_get_scope_key_by_id_returns_404_when_missing(client) -> None:
    """GET /capability-scope-keys/{id} returns 404 when the id doesn't exist."""
    response = await client.get(f'{_BASE}/99999')
    assert response.status_code == 404
    assert response.json()['detail'] == 'Capability scope key not found'


@pytest.mark.asyncio
async def test_patch_scope_key_returns_200_and_updates_name(client) -> None:
    """PATCH /capability-scope-keys/{id} updates name and returns 200."""
    create_resp = await client.post(_BASE, json={'code': 'DEPARTMENT', 'name': 'Original'})
    assert create_resp.status_code == 201
    sk_id = create_resp.json()['id']
    original_code = create_resp.json()['code']

    patch_resp = await client.patch(f'{_BASE}/{sk_id}', json={'name': 'Updated Name'})
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body['name'] == 'Updated Name'
    assert body['code'] == original_code  # code immutable


@pytest.mark.asyncio
async def test_post_deactivate_sets_is_active_false(client) -> None:
    """POST /capability-scope-keys/{id}/deactivate sets is_active to False."""
    create_resp = await client.post(_BASE, json={'code': 'PROJECT', 'name': 'Project'})
    assert create_resp.status_code == 201
    sk_id = create_resp.json()['id']
    assert create_resp.json()['is_active'] is True

    deactivate_resp = await client.post(f'{_BASE}/{sk_id}/deactivate')
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.json()['is_active'] is False


@pytest.mark.asyncio
async def test_list_scope_keys_filters_by_is_active(client) -> None:
    """GET /capability-scope-keys?is_active=true/false filters correctly."""
    await client.post(_BASE, json={'code': 'REGION', 'name': 'Region'})
    await client.post(_BASE, json={'code': 'PROGRAM', 'name': 'Program'})
    create_resp = await client.post(_BASE, json={'code': 'TENANT', 'name': 'Tenant'})
    sk_id = create_resp.json()['id']
    await client.post(f'{_BASE}/{sk_id}/deactivate')

    active_resp = await client.get(f'{_BASE}?is_active=true')
    assert active_resp.status_code == 200
    active_codes = [sk['code'] for sk in active_resp.json()]
    assert 'REGION' in active_codes
    assert 'PROGRAM' in active_codes
    assert 'TENANT' not in active_codes

    inactive_resp = await client.get(f'{_BASE}?is_active=false')
    assert inactive_resp.status_code == 200
    inactive_codes = [sk['code'] for sk in inactive_resp.json()]
    assert 'TENANT' in inactive_codes

    all_resp = await client.get(_BASE)
    assert all_resp.status_code == 200
    assert len(all_resp.json()) >= 3
