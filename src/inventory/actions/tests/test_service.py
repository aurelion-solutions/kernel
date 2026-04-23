# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for ActionService."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.actions.models import Action
from src.inventory.actions.schemas import ActionRead
from src.inventory.actions.service import ActionService
from src.platform.logs.service import NoOpLogService

_SEED_ROWS = [
    ('read', 'Observe a resource without modifying it.'),
    ('write', 'Modify a resource.'),
    ('execute', 'Trigger an operation on a resource.'),
    ('approve', 'Approve a request or transaction.'),
    ('admin', 'Administer configuration of a resource.'),
    ('use', 'Consume a resource as a functional user.'),
    ('own', 'Ownership-level control of a resource.'),
]


async def _seed_vocabulary(session: AsyncSession) -> None:
    session.add_all([Action(slug=slug, description=desc) for slug, desc in _SEED_ROWS])
    await session.flush()


@pytest_asyncio.fixture
async def seeded_service(session_factory):
    async with session_factory() as session:
        await _seed_vocabulary(session)
        yield ActionService(session, NoOpLogService())


@pytest.mark.asyncio
async def test_list_actions_returns_seven_rows_in_id_order(seeded_service) -> None:
    service = seeded_service
    result = await service.list_actions()
    assert len(result) == 7
    assert [a.slug for a in result] == ['read', 'write', 'execute', 'approve', 'admin', 'use', 'own']


@pytest.mark.asyncio
async def test_list_actions_returns_action_read_instances(seeded_service) -> None:
    service = seeded_service
    result = await service.list_actions()
    assert all(isinstance(a, ActionRead) for a in result)
    first = result[0]
    assert first.id > 0
    assert first.slug == 'read'
    assert first.description is not None
    assert first.created_at is not None


@pytest.mark.asyncio
async def test_list_actions_empty_when_table_empty(session_factory) -> None:
    async with session_factory() as session:
        service = ActionService(session, NoOpLogService())
        result = await service.list_actions()
    assert result == []


@pytest.mark.asyncio
async def test_get_action_by_slug_returns_existing(seeded_service) -> None:
    service = seeded_service
    result = await service.get_action_by_slug('read')
    assert result is not None
    assert result.slug == 'read'
    assert result.description == 'Observe a resource without modifying it.'
    assert result.id > 0


@pytest.mark.asyncio
async def test_get_action_by_slug_returns_none_when_missing(seeded_service) -> None:
    service = seeded_service
    result = await service.get_action_by_slug('nonexistent_slug')
    assert result is None


@pytest.mark.asyncio
async def test_get_action_by_slug_is_case_sensitive(seeded_service) -> None:
    service = seeded_service
    result = await service.get_action_by_slug('READ')
    assert result is None
