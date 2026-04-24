# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /actions routes."""

from __future__ import annotations

import pytest

_BASE = '/api/v0/actions'

# conftest._REF_ACTIONS_SEED pre-seeds 10 actions (7 original + 3 from Phase 12).
# Tests rely on this seeding — do not re-seed manually.


@pytest.mark.asyncio
async def test_list_actions_returns_seeded_rows(client) -> None:
    response = await client.get(_BASE)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    slugs = [r['slug'] for r in body]
    assert 'read' in slugs
    assert 'write' in slugs
    assert 'admin' in slugs
    assert 'use' in slugs
    assert 'own' in slugs
    assert len(body) >= 7


@pytest.mark.asyncio
async def test_list_actions_returns_200_with_non_empty_list(client) -> None:
    response = await client.get(_BASE)
    assert response.status_code == 200
    assert len(response.json()) > 0


@pytest.mark.asyncio
async def test_get_action_returns_existing(client) -> None:
    # conftest seeds 'read' with description 'Read access'
    response = await client.get(f'{_BASE}/read')
    assert response.status_code == 200
    body = response.json()
    assert body['slug'] == 'read'
    assert body['description'] == 'Read access'
    assert 'id' in body
    assert 'created_at' in body


@pytest.mark.asyncio
async def test_get_action_returns_404_for_unknown_slug(client) -> None:
    response = await client.get(f'{_BASE}/nonexistent_slug_xyz')
    assert response.status_code == 404
    assert response.json()['detail'] == 'Action not found'


@pytest.mark.asyncio
async def test_get_action_is_case_sensitive(client) -> None:
    response = await client.get(f'{_BASE}/READ')
    assert response.status_code == 404
