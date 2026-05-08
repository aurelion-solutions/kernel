# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API route tests for lake_migration endpoints."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_post_starts_run_returns_202(client: AsyncClient) -> None:
    """POST /api/v0/lake-migrations with valid dataset returns 202."""
    resp = await client.post(
        '/api/v0/lake-migrations',
        json={'dataset': 'access_artifacts', 'batch_size': 5000},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body['dataset'] == 'access_artifacts'
    assert body['status'] == 'pending'
    assert 'id' in body


@pytest.mark.asyncio
async def test_get_run_returns_200(client: AsyncClient) -> None:
    """GET /api/v0/lake-migrations/{id} returns the run."""
    post_resp = await client.post(
        '/api/v0/lake-migrations',
        json={'dataset': 'access_artifacts', 'batch_size': 5000},
    )
    assert post_resp.status_code == 202
    run_id = post_resp.json()['id']

    get_resp = await client.get(f'/api/v0/lake-migrations/{run_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == run_id


@pytest.mark.asyncio
async def test_get_nonexistent_run_returns_404(client: AsyncClient) -> None:
    """GET /api/v0/lake-migrations/{nonexistent} returns 404."""
    fake_id = uuid.uuid4()
    resp = await client.get(f'/api/v0/lake-migrations/{fake_id}')
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_invalid_dataset_returns_422(client: AsyncClient) -> None:
    """POST with unknown dataset name returns 422."""
    resp = await client.post(
        '/api/v0/lake-migrations',
        json={'dataset': 'banana', 'batch_size': 5000},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_runs_returns_200(client: AsyncClient) -> None:
    """GET /api/v0/lake-migrations returns 200 with items list."""
    await client.post(
        '/api/v0/lake-migrations',
        json={'dataset': 'access_artifacts', 'batch_size': 5000},
    )
    resp = await client.get('/api/v0/lake-migrations')
    assert resp.status_code == 200
    body = resp.json()
    assert 'items' in body
    assert isinstance(body['items'], list)
    assert len(body['items']) >= 1


@pytest.mark.asyncio
async def test_post_all_returns_list_of_two(client: AsyncClient) -> None:
    """POST with dataset='all' returns a list of two run objects."""
    resp = await client.post(
        '/api/v0/lake-migrations',
        json={'dataset': 'all', 'batch_size': 5000},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    datasets = {r['dataset'] for r in body}
    assert datasets == {'access_artifacts', 'access_facts'}
