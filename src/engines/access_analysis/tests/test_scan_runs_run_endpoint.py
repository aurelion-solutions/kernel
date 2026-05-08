# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for POST /scan-runs/{id}/run endpoint."""

from __future__ import annotations

import pytest

_BASE = '/api/v0/scan-runs'


@pytest.mark.asyncio
async def test_run_endpoint_pending_run_returns_200(client) -> None:
    """POST /scan-runs/{id}/run on a pending run returns 200 with status=completed."""
    create_resp = await client.post(_BASE, json={'triggered_by': 'manual'})
    assert create_resp.status_code == 201
    run_id = create_resp.json()['id']

    run_resp = await client.post(f'{_BASE}/{run_id}/run')
    assert run_resp.status_code == 200
    body = run_resp.json()
    assert body['status'] == 'completed'
    assert 'findings_total' in body
    assert 'findings_created_count' in body
    assert 'findings_reused_count' in body


@pytest.mark.asyncio
async def test_run_endpoint_nonexistent_id_returns_404(client) -> None:
    """POST /scan-runs/{id}/run with non-existent id returns 404."""
    resp = await client.post(f'{_BASE}/99999999/run')
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_endpoint_running_status_returns_409(client) -> None:
    """POST /scan-runs/{id}/run on a running run returns 409."""
    create_resp = await client.post(_BASE, json={'triggered_by': 'manual'})
    run_id = create_resp.json()['id']

    # Manually transition to running
    await client.patch(f'{_BASE}/{run_id}/status', json={'status': 'running'})

    resp = await client.post(f'{_BASE}/{run_id}/run')
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_run_endpoint_completed_status_returns_409(client) -> None:
    """POST /scan-runs/{id}/run on a completed run returns 409."""
    create_resp = await client.post(_BASE, json={'triggered_by': 'manual'})
    run_id = create_resp.json()['id']

    # Run it once to completion
    await client.post(f'{_BASE}/{run_id}/run')

    # Try again — should get 409
    resp = await client.post(f'{_BASE}/{run_id}/run')
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_run_endpoint_counters_present_in_response(client) -> None:
    """Response body includes findings_created_count and findings_reused_count."""
    create_resp = await client.post(_BASE, json={'triggered_by': 'api'})
    run_id = create_resp.json()['id']

    run_resp = await client.post(f'{_BASE}/{run_id}/run')
    body = run_resp.json()

    assert isinstance(body['findings_created_count'], int)
    assert isinstance(body['findings_reused_count'], int)
    assert body['findings_created_count'] >= 0
    assert body['findings_reused_count'] >= 0
