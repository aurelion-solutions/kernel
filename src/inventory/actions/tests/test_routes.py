# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /actions routes."""

from __future__ import annotations

import pytest
from src.inventory.actions.models import Action

_BASE = '/api/v0/actions'

_SEED_ROWS = [
    ('read', 'Observe a resource without modifying it.'),
    ('write', 'Modify a resource.'),
    ('execute', 'Trigger an operation on a resource.'),
    ('approve', 'Approve a request or transaction.'),
    ('admin', 'Administer configuration of a resource.'),
    ('use', 'Consume a resource as a functional user.'),
    ('own', 'Ownership-level control of a resource.'),
]


async def _seed_vocabulary(session_factory) -> None:
    async with session_factory() as session:
        session.add_all([Action(slug=s, description=d) for s, d in _SEED_ROWS])
        await session.commit()


@pytest.mark.asyncio
async def test_list_actions_returns_seven_rows_in_id_order(client, session_factory) -> None:
    await _seed_vocabulary(session_factory)
    response = await client.get(_BASE)
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 7
    assert [r['slug'] for r in body] == ['read', 'write', 'execute', 'approve', 'admin', 'use', 'own']


@pytest.mark.asyncio
async def test_list_actions_returns_empty_when_vocabulary_empty(client) -> None:
    response = await client.get(_BASE)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_action_returns_existing(client, session_factory) -> None:
    await _seed_vocabulary(session_factory)
    response = await client.get(f'{_BASE}/read')
    assert response.status_code == 200
    body = response.json()
    assert body['slug'] == 'read'
    assert body['description'] == 'Observe a resource without modifying it.'
    assert 'id' in body
    assert 'created_at' in body


@pytest.mark.asyncio
async def test_get_action_returns_404_for_unknown_slug(client, session_factory) -> None:
    await _seed_vocabulary(session_factory)
    response = await client.get(f'{_BASE}/nonexistent_slug')
    assert response.status_code == 404
    assert response.json()['detail'] == 'Action not found'


@pytest.mark.asyncio
async def test_get_action_is_case_sensitive(client, session_factory) -> None:
    await _seed_vocabulary(session_factory)
    response = await client.get(f'{_BASE}/READ')
    assert response.status_code == 404
