# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /capabilities routes."""

from __future__ import annotations

import pytest

_BASE = '/api/v0/capabilities'


@pytest.mark.asyncio
async def test_post_capability_returns_201(client) -> None:
    """POST /capabilities with valid payload returns 201 and CapabilityRead body."""
    response = await client.post(
        _BASE,
        json={
            'slug': 'approve_payment',
            'name': 'Approve Payment',
            'description': 'Approve a payment transaction.',
            'is_active': True,
            'created_by': 'alice@example.com',
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body['slug'] == 'approve_payment'
    assert body['name'] == 'Approve Payment'
    assert body['description'] == 'Approve a payment transaction.'
    assert body['is_active'] is True
    assert body['created_by'] == 'alice@example.com'
    assert 'id' in body
    assert body['id'] > 0
    assert 'created_at' in body


@pytest.mark.asyncio
async def test_post_capability_duplicate_slug_returns_409(client) -> None:
    """POST /capabilities with a duplicate slug returns 409."""
    payload = {'slug': 'create_vendor', 'name': 'Create Vendor'}
    first = await client.post(_BASE, json=payload)
    assert first.status_code == 201

    second = await client.post(_BASE, json={**payload, 'name': 'Create Vendor Dup'})
    assert second.status_code == 409
    assert 'create_vendor' in second.json()['detail']


@pytest.mark.asyncio
async def test_get_capability_by_id_returns_404_when_missing(client) -> None:
    """GET /capabilities/{id} returns 404 when the id doesn't exist."""
    response = await client.get(f'{_BASE}/99999')
    assert response.status_code == 404
    assert response.json()['detail'] == 'Capability not found'


@pytest.mark.asyncio
async def test_patch_capability_returns_200_and_updates_name(client) -> None:
    """PATCH /capabilities/{id} updates name and returns 200."""
    create_resp = await client.post(_BASE, json={'slug': 'patch_me', 'name': 'Original'})
    assert create_resp.status_code == 201
    cap_id = create_resp.json()['id']
    original_slug = create_resp.json()['slug']

    patch_resp = await client.patch(f'{_BASE}/{cap_id}', json={'name': 'Updated Name'})
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body['name'] == 'Updated Name'
    assert body['slug'] == original_slug  # slug immutable


@pytest.mark.asyncio
async def test_post_deactivate_sets_is_active_false(client) -> None:
    """POST /capabilities/{id}/deactivate sets is_active to False."""
    create_resp = await client.post(_BASE, json={'slug': 'deactivate_me', 'name': 'To Deactivate'})
    assert create_resp.status_code == 201
    cap_id = create_resp.json()['id']
    assert create_resp.json()['is_active'] is True

    deactivate_resp = await client.post(f'{_BASE}/{cap_id}/deactivate')
    assert deactivate_resp.status_code == 200
    assert deactivate_resp.json()['is_active'] is False


@pytest.mark.asyncio
async def test_list_capabilities_filters_by_is_active(client) -> None:
    """GET /capabilities?is_active=true/false filters correctly."""
    await client.post(_BASE, json={'slug': 'active_cap_1', 'name': 'Active 1'})
    await client.post(_BASE, json={'slug': 'active_cap_2', 'name': 'Active 2'})
    create_resp = await client.post(_BASE, json={'slug': 'to_deactivate', 'name': 'Will Deactivate'})
    cap_id = create_resp.json()['id']
    await client.post(f'{_BASE}/{cap_id}/deactivate')

    active_resp = await client.get(f'{_BASE}?is_active=true')
    assert active_resp.status_code == 200
    active_slugs = [c['slug'] for c in active_resp.json()]
    assert 'active_cap_1' in active_slugs
    assert 'active_cap_2' in active_slugs
    assert 'to_deactivate' not in active_slugs

    inactive_resp = await client.get(f'{_BASE}?is_active=false')
    assert inactive_resp.status_code == 200
    inactive_slugs = [c['slug'] for c in inactive_resp.json()]
    assert 'to_deactivate' in inactive_slugs

    all_resp = await client.get(_BASE)
    assert all_resp.status_code == 200
    assert len(all_resp.json()) >= 3


@pytest.mark.asyncio
async def test_get_capability_returns_correct_data(client) -> None:
    """GET /capabilities/{id} returns the correct capability."""
    create_resp = await client.post(
        _BASE,
        json={
            'slug': 'get_by_id_test',
            'name': 'Get By Id Test',
            'description': 'Test description',
        },
    )
    assert create_resp.status_code == 201
    cap_id = create_resp.json()['id']

    get_resp = await client.get(f'{_BASE}/{cap_id}')
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body['id'] == cap_id
    assert body['slug'] == 'get_by_id_test'
    assert body['description'] == 'Test description'


@pytest.mark.asyncio
async def test_patch_missing_id_returns_404(client) -> None:
    """PATCH /capabilities/{id} returns 404 when id doesn't exist."""
    response = await client.patch(f'{_BASE}/99999', json={'name': 'Whatever'})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_deactivate_missing_id_returns_404(client) -> None:
    """POST /capabilities/{id}/deactivate returns 404 when id doesn't exist."""
    response = await client.post(f'{_BASE}/99999/deactivate')
    assert response.status_code == 404
