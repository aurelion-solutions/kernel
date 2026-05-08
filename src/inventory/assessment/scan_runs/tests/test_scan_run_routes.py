# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /scan-runs routes."""

from __future__ import annotations

import uuid

import pytest

_BASE = '/api/v0/scan-runs'


def _manual_body(**kwargs) -> dict:
    return {'triggered_by': 'manual', **kwargs}


@pytest.mark.asyncio
async def test_post_scan_run_valid_returns_201(client) -> None:
    resp = await client.post(_BASE, json=_manual_body())
    assert resp.status_code == 201
    body = resp.json()
    assert body['status'] == 'pending'
    assert body['triggered_by'] == 'manual'
    assert body['started_at'] is None
    assert body['findings_total'] == 0
    assert body['findings_by_severity'] == {}
    assert 'id' in body


@pytest.mark.asyncio
async def test_post_scan_run_unknown_scope_subject_id_returns_422(client) -> None:
    resp = await client.post(
        _BASE,
        json=_manual_body(scope_subject_id=str(uuid.uuid4())),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_scan_runs_status_filter_returns_only_pending(client) -> None:
    resp1 = await client.post(_BASE, json=_manual_body())
    run_id = resp1.json()['id']

    # Start it so it becomes running
    await client.patch(f'{_BASE}/{run_id}/status', json={'status': 'running'})

    resp2 = await client.post(_BASE, json=_manual_body())
    pending_id = resp2.json()['id']

    list_resp = await client.get(f'{_BASE}?status=pending')
    assert list_resp.status_code == 200
    ids = [r['id'] for r in list_resp.json()]
    assert pending_id in ids
    assert run_id not in ids


@pytest.mark.asyncio
async def test_get_scan_run_by_id_missing_returns_404(client) -> None:
    resp = await client.get(f'{_BASE}/99999')
    assert resp.status_code == 404
    assert 'not found' in resp.json()['detail'].lower()


@pytest.mark.asyncio
async def test_patch_scan_run_status_pending_to_running_returns_200(client) -> None:
    create_resp = await client.post(_BASE, json=_manual_body())
    run_id = create_resp.json()['id']

    resp = await client.patch(f'{_BASE}/{run_id}/status', json={'status': 'running'})
    assert resp.status_code == 200
    body = resp.json()
    assert body['status'] == 'running'
    assert body['started_at'] is not None

    # second call: running → running → 422
    resp2 = await client.patch(f'{_BASE}/{run_id}/status', json={'status': 'running'})
    assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_patch_scan_run_status_running_to_failed_without_error_message_returns_422(client) -> None:
    create_resp = await client.post(_BASE, json=_manual_body())
    run_id = create_resp.json()['id']
    await client.patch(f'{_BASE}/{run_id}/status', json={'status': 'running'})

    resp = await client.patch(f'{_BASE}/{run_id}/status', json={'status': 'failed'})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_scan_run_status_extra_field_returns_422(client) -> None:
    create_resp = await client.post(_BASE, json=_manual_body())
    run_id = create_resp.json()['id']

    resp = await client.patch(
        f'{_BASE}/{run_id}/status',
        json={'status': 'running', 'unknown_field': 'value'},
    )
    assert resp.status_code == 422
