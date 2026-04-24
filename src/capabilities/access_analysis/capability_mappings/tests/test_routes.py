# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /capability-mappings routes."""

from __future__ import annotations

import pytest

_BASE = '/api/v0/capability-mappings'
_CAPS_BASE = '/api/v0/capabilities'
_SCOPE_KEYS_BASE = '/api/v0/capability-scope-keys'
_APPS_BASE = '/api/v0/applications'


async def _create_capability(client, slug: str = None) -> int:
    import uuid

    slug = slug or f'cap_{uuid.uuid4().hex[:8]}'
    resp = await client.post(_CAPS_BASE, json={'slug': slug, 'name': slug})
    assert resp.status_code == 201
    return resp.json()['id']


async def _create_scope_key(client, code: str = None) -> int:
    import uuid

    code = code or f'SK_{uuid.uuid4().hex[:8].upper()}'
    resp = await client.post(_SCOPE_KEYS_BASE, json={'code': code, 'name': code})
    assert resp.status_code == 201
    return resp.json()['id']


async def _create_mapping(client, capability_id: int, scope_key_id: int, **overrides) -> dict:
    payload = {
        'capability_id': capability_id,
        'scope_key_id': scope_key_id,
        'resource_kind': 'role',
        'scope_value_source': {'kind': 'constant', 'value': 'admin'},
        **overrides,
    }
    resp = await client.post(_BASE, json=payload)
    return resp


@pytest.mark.asyncio
async def test_post_capability_mapping_returns_201_with_resource_kind_match(client) -> None:
    """POST /capability-mappings with resource_kind returns 201 and correct body."""
    cap_id = await _create_capability(client)
    sk_id = await _create_scope_key(client)

    resp = await _create_mapping(client, cap_id, sk_id)
    assert resp.status_code == 201
    body = resp.json()
    assert body['capability_id'] == cap_id
    assert body['scope_key_id'] == sk_id
    assert body['resource_kind'] == 'role'
    assert body['resource_id'] is None
    assert body['resource_path_glob'] is None
    assert body['is_active'] is True
    assert 'id' in body
    assert body['id'] > 0


@pytest.mark.asyncio
async def test_post_capability_mapping_with_unknown_action_slug_returns_422(client) -> None:
    """POST /capability-mappings with nonexistent action_slug returns 422."""
    cap_id = await _create_capability(client)
    sk_id = await _create_scope_key(client)

    resp = await _create_mapping(client, cap_id, sk_id, action_slug='nonexistent_slug')
    assert resp.status_code == 422
    assert 'nonexistent_slug' in resp.json()['detail']


@pytest.mark.asyncio
async def test_post_capability_mapping_with_two_resource_match_fields_returns_422(client) -> None:
    """POST /capability-mappings with both resource_id and resource_kind returns 422 (Pydantic XOR)."""
    import uuid

    cap_id = await _create_capability(client)
    sk_id = await _create_scope_key(client)

    resp = await client.post(
        _BASE,
        json={
            'capability_id': cap_id,
            'scope_key_id': sk_id,
            'resource_id': str(uuid.uuid4()),
            'resource_kind': 'role',  # two set — Pydantic rejects
            'scope_value_source': {'kind': 'constant', 'value': 'x'},
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_capability_mapping_by_id_returns_404_when_missing(client) -> None:
    """GET /capability-mappings/{id} returns 404 when not found."""
    resp = await client.get(f'{_BASE}/99999')
    assert resp.status_code == 404
    assert resp.json()['detail'] == 'Capability mapping not found'


@pytest.mark.asyncio
async def test_patch_capability_mapping_returns_200_and_flips_is_active(client) -> None:
    """PATCH /capability-mappings/{id} flips is_active and returns 200."""
    cap_id = await _create_capability(client)
    sk_id = await _create_scope_key(client)
    create_resp = await _create_mapping(client, cap_id, sk_id)
    assert create_resp.status_code == 201
    mapping_id = create_resp.json()['id']

    patch_resp = await client.patch(f'{_BASE}/{mapping_id}', json={'is_active': False})
    assert patch_resp.status_code == 200
    assert patch_resp.json()['is_active'] is False


@pytest.mark.asyncio
async def test_delete_capability_mapping_returns_204(client) -> None:
    """DELETE /capability-mappings/{id} returns 204 and follow-up GET returns 404."""
    cap_id = await _create_capability(client)
    sk_id = await _create_scope_key(client)
    create_resp = await _create_mapping(client, cap_id, sk_id)
    assert create_resp.status_code == 201
    mapping_id = create_resp.json()['id']

    delete_resp = await client.delete(f'{_BASE}/{mapping_id}')
    assert delete_resp.status_code == 204

    get_resp = await client.get(f'{_BASE}/{mapping_id}')
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_list_capability_mappings_filters_by_scope_key_id(client) -> None:
    """GET /capability-mappings?scope_key_id=X filters by scope_key_id."""

    cap_id = await _create_capability(client)
    sk_id_a = await _create_scope_key(client)
    sk_id_b = await _create_scope_key(client)

    # Create two mappings for scope key A, one for scope key B
    await _create_mapping(client, cap_id, sk_id_a, resource_kind='role')
    await _create_mapping(client, cap_id, sk_id_a, resource_kind=None, resource_path_glob='/api/*')
    await _create_mapping(client, cap_id, sk_id_b, resource_kind='account')

    resp = await client.get(f'{_BASE}?scope_key_id={sk_id_a}')
    assert resp.status_code == 200
    bodies = resp.json()
    assert len(bodies) == 2
    assert all(m['scope_key_id'] == sk_id_a for m in bodies)

    resp_b = await client.get(f'{_BASE}?scope_key_id={sk_id_b}')
    assert resp_b.status_code == 200
    assert len(resp_b.json()) == 1


@pytest.mark.asyncio
async def test_patch_missing_id_returns_404(client) -> None:
    """PATCH /capability-mappings/99999 returns 404."""
    resp = await client.patch(f'{_BASE}/99999', json={'is_active': False})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_capability_mappings_returns_200(client) -> None:
    """GET /capability-mappings returns 200 with a list."""
    resp = await client.get(_BASE)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
