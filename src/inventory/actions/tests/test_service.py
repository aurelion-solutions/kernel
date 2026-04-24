# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for ActionService."""

from __future__ import annotations

import pytest
import pytest_asyncio
from src.inventory.actions.schemas import ActionRead
from src.inventory.actions.service import ActionService
from src.platform.logs.service import NoOpLogService


@pytest_asyncio.fixture
async def seeded_service(session_factory):
    # ref_actions are seeded by the engine fixture in conftest.py (_REF_ACTIONS_SEED).
    # Do not re-insert here — the UNIQUE constraint on slug would fail.
    async with session_factory() as session:
        yield ActionService(session, NoOpLogService())


@pytest.mark.asyncio
async def test_list_actions_returns_seeded_rows_in_id_order(seeded_service) -> None:
    # conftest._REF_ACTIONS_SEED contains 10 actions (original 7 + 3 added in Phase 12)
    service = seeded_service
    result = await service.list_actions()
    slugs = [a.slug for a in result]
    assert 'read' in slugs
    assert 'write' in slugs
    assert 'admin' in slugs
    assert 'use' in slugs
    assert 'own' in slugs
    assert len(result) >= 7


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
async def test_get_action_by_slug_returns_existing(seeded_service) -> None:
    # conftest seeds 'read' with description 'Read access'
    service = seeded_service
    result = await service.get_action_by_slug('read')
    assert result is not None
    assert result.slug == 'read'
    assert result.description == 'Read access'
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
