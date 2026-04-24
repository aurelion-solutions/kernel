# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /mitigation-controls routes."""

from __future__ import annotations

import pytest

_BASE = '/api/v0/mitigation-controls'

_VALID_BODY = {
    'code': 'QUARTERLY_ATTESTATION',
    'name': 'Quarterly access attestation',
    'type': 'attestation',
    'description': 'Periodic attestation.',
    'is_active': True,
    'created_by': 'alice@example.com',
}


@pytest.mark.asyncio
async def test_post_mitigation_control_returns_201(client) -> None:
    """POST /mitigation-controls with valid body returns 201 and MitigationControlRead shape."""
    response = await client.post(_BASE, json=_VALID_BODY)
    assert response.status_code == 201
    body = response.json()
    assert body['code'] == 'QUARTERLY_ATTESTATION'
    assert body['name'] == 'Quarterly access attestation'
    assert body['type'] == 'attestation'
    assert body['is_active'] is True
    assert body['created_by'] == 'alice@example.com'
    assert 'id' in body
    assert body['id'] > 0
    assert 'created_at' in body


@pytest.mark.asyncio
async def test_post_mitigation_control_duplicate_code_returns_409(client) -> None:
    """POST /mitigation-controls with duplicate code returns 409."""
    first = await client.post(_BASE, json=_VALID_BODY)
    assert first.status_code == 201

    second = await client.post(_BASE, json={**_VALID_BODY, 'name': 'Duplicate'})
    assert second.status_code == 409
    assert 'QUARTERLY_ATTESTATION' in second.json()['detail']


@pytest.mark.asyncio
async def test_post_mitigation_control_extra_field_returns_422(client) -> None:
    """POST /mitigation-controls with unknown extra field returns 422 (extra='forbid')."""
    response = await client.post(_BASE, json={**_VALID_BODY, 'code': 'EXTRA_FIELD_1', 'unexpected': 'value'})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_mitigation_controls_no_filter_returns_200(client) -> None:
    """GET /mitigation-controls returns 200 with a list."""
    await client.post(_BASE, json={**_VALID_BODY, 'code': 'LIST_TEST_1'})
    response = await client.get(_BASE)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_get_mitigation_controls_filter_by_is_active_false(client) -> None:
    """GET /mitigation-controls?is_active=false returns only inactive rows."""
    create_resp = await client.post(_BASE, json={**_VALID_BODY, 'code': 'FILTER_ACTIVE_1'})
    assert create_resp.status_code == 201
    ctrl_id = create_resp.json()['id']

    await client.post(f'{_BASE}/{ctrl_id}/deactivate')

    response = await client.get(f'{_BASE}?is_active=false')
    assert response.status_code == 200
    codes = [c['code'] for c in response.json()]
    assert 'FILTER_ACTIVE_1' in codes
    assert all(not c['is_active'] for c in response.json())


@pytest.mark.asyncio
async def test_get_mitigation_controls_filter_by_type(client) -> None:
    """GET /mitigation-controls?type=attestation returns only attestation controls."""
    await client.post(
        _BASE,
        json={**_VALID_BODY, 'code': 'ATTEST_TYPE_1', 'type': 'attestation'},
    )
    await client.post(
        _BASE,
        json={**_VALID_BODY, 'code': 'DUAL_TYPE_1', 'type': 'dual_approval'},
    )

    response = await client.get(f'{_BASE}?type=attestation')
    assert response.status_code == 200
    types = [c['type'] for c in response.json()]
    assert all(t == 'attestation' for t in types)
    codes = [c['code'] for c in response.json()]
    assert 'ATTEST_TYPE_1' in codes
    assert 'DUAL_TYPE_1' not in codes


@pytest.mark.asyncio
async def test_get_mitigation_control_by_id_missing_returns_404(client) -> None:
    """GET /mitigation-controls/{id} returns 404 when id doesn't exist."""
    response = await client.get(f'{_BASE}/99999')
    assert response.status_code == 404
    assert response.json()['detail'] == 'MitigationControl not found'


@pytest.mark.asyncio
async def test_patch_mitigation_control_valid_returns_200(client) -> None:
    """PATCH /mitigation-controls/{id} with valid fields returns 200 with updated values."""
    create_resp = await client.post(_BASE, json={**_VALID_BODY, 'code': 'PATCH_TEST_1'})
    assert create_resp.status_code == 201
    ctrl_id = create_resp.json()['id']
    original_code = create_resp.json()['code']

    patch_resp = await client.patch(
        f'{_BASE}/{ctrl_id}',
        json={'name': 'Updated Name', 'type': 'dual_approval'},
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body['name'] == 'Updated Name'
    assert body['type'] == 'dual_approval'
    assert body['code'] == original_code  # code immutable


@pytest.mark.asyncio
async def test_patch_mitigation_control_with_code_returns_422(client) -> None:
    """PATCH /mitigation-controls/{id} with 'code' field returns 422 (extra='forbid')."""
    create_resp = await client.post(_BASE, json={**_VALID_BODY, 'code': 'PATCH_CODE_TEST_1'})
    assert create_resp.status_code == 201
    ctrl_id = create_resp.json()['id']

    patch_resp = await client.patch(f'{_BASE}/{ctrl_id}', json={'code': 'NEW_CODE'})
    assert patch_resp.status_code == 422


@pytest.mark.asyncio
async def test_deactivate_mitigation_control_returns_200_and_is_idempotent(client) -> None:
    """POST /mitigation-controls/{id}/deactivate returns 200 with is_active=False; second call also 200."""
    create_resp = await client.post(_BASE, json={**_VALID_BODY, 'code': 'DEACT_TEST_1'})
    assert create_resp.status_code == 201
    ctrl_id = create_resp.json()['id']
    assert create_resp.json()['is_active'] is True

    deact_resp1 = await client.post(f'{_BASE}/{ctrl_id}/deactivate')
    assert deact_resp1.status_code == 200
    assert deact_resp1.json()['is_active'] is False

    # second call — idempotent, still 200
    deact_resp2 = await client.post(f'{_BASE}/{ctrl_id}/deactivate')
    assert deact_resp2.status_code == 200
    assert deact_resp2.json()['is_active'] is False
