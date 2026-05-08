# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /sod-rules routes."""

from __future__ import annotations

import pytest

_BASE = '/api/v0/sod-rules'


async def _create_scope_key(client) -> int:
    """Create a CapabilityScopeKey via DB and return its id."""
    # Use the capabilities/scope-keys endpoint if available, otherwise raw SQL
    resp = await client.post(
        '/api/v0/capability-scope-keys',
        json={'code': 'ROUTE_TEST_SK', 'name': 'Route Test Scope Key'},
    )
    if resp.status_code in (200, 201):
        return resp.json()['id']
    # Already exists — get by listing
    list_resp = await client.get('/api/v0/capability-scope-keys')
    for sk in list_resp.json():
        if sk['code'] == 'ROUTE_TEST_SK':
            return sk['id']
    raise RuntimeError('Could not get scope key id')


def _global_rule(code: str = 'RT-001', name: str = 'Route Test Rule') -> dict:
    return {
        'code': code,
        'name': name,
        'severity': 'high',
        'scope_mode': 'global',
    }


@pytest.mark.asyncio
async def test_post_sod_rule_returns_201(client) -> None:
    response = await client.post(_BASE, json=_global_rule('POST-001'))
    assert response.status_code == 201
    body = response.json()
    assert body['code'] == 'POST-001'
    assert body['severity'] == 'high'
    assert body['scope_mode'] == 'global'
    assert body['is_enabled'] is True
    assert 'id' in body


@pytest.mark.asyncio
async def test_post_sod_rule_duplicate_code_returns_409(client) -> None:
    await client.post(_BASE, json=_global_rule('DUP-RT-001'))
    resp = await client.post(_BASE, json=_global_rule('DUP-RT-001'))
    assert resp.status_code == 409
    assert 'DUP-RT-001' in resp.json()['detail']


@pytest.mark.asyncio
async def test_post_sod_rule_scope_invariant_violation_returns_422(client) -> None:
    resp = await client.post(
        _BASE,
        json={
            'code': 'INV-RT-001',
            'name': 'Bad Rule',
            'severity': 'high',
            'scope_mode': 'by_scope_key',
            # missing scope_key_id
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_sod_rule_unknown_scope_key_id_returns_422(client) -> None:
    resp = await client.post(
        _BASE,
        json={
            'code': 'INV-RT-002',
            'name': 'Bad Rule',
            'severity': 'high',
            'scope_mode': 'by_scope_key',
            'scope_key_id': 999999,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_sod_rules_with_filters(client) -> None:
    await client.post(_BASE, json={**_global_rule('FILT-001'), 'severity': 'critical'})
    await client.post(_BASE, json={**_global_rule('FILT-002'), 'severity': 'low'})

    resp = await client.get(f'{_BASE}?severity=critical')
    assert resp.status_code == 200
    codes = [r['code'] for r in resp.json()]
    assert 'FILT-001' in codes
    assert 'FILT-002' not in codes


@pytest.mark.asyncio
async def test_get_sod_rule_by_id_missing_returns_404(client) -> None:
    resp = await client.get(f'{_BASE}/99999')
    assert resp.status_code == 404
    assert 'not found' in resp.json()['detail'].lower()


@pytest.mark.asyncio
async def test_patch_sod_rule_missing_returns_404(client) -> None:
    resp = await client.patch(f'{_BASE}/99999', json={'name': 'Whatever'})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_sod_rule_with_code_in_body_returns_422(client) -> None:
    create_resp = await client.post(_BASE, json=_global_rule('PATCH-CODE-TEST'))
    rule_id = create_resp.json()['id']
    resp = await client.patch(f'{_BASE}/{rule_id}', json={'code': 'NEW-CODE'})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_sod_rule_invalid_scope_toggle_returns_422(client) -> None:
    create_resp = await client.post(_BASE, json=_global_rule('PATCH-INV-RT'))
    rule_id = create_resp.json()['id']
    # Try to switch to by_scope_key without providing scope_key_id
    resp = await client.patch(f'{_BASE}/{rule_id}', json={'scope_mode': 'by_scope_key'})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_deactivate_returns_200_and_is_enabled_false(client) -> None:
    create_resp = await client.post(_BASE, json=_global_rule('DEACT-RT-001'))
    rule_id = create_resp.json()['id']

    deact_resp = await client.post(f'{_BASE}/{rule_id}/deactivate')
    assert deact_resp.status_code == 200
    assert deact_resp.json()['is_enabled'] is False


@pytest.mark.asyncio
async def test_post_deactivate_idempotent(client) -> None:
    create_resp = await client.post(_BASE, json=_global_rule('DEACT-RT-002'))
    rule_id = create_resp.json()['id']

    r1 = await client.post(f'{_BASE}/{rule_id}/deactivate')
    r2 = await client.post(f'{_BASE}/{rule_id}/deactivate')
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()['is_enabled'] is False
    assert r2.json()['is_enabled'] is False
