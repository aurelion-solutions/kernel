# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for CapabilityGrant read-only routes."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.engines.access_analysis.services.capability_projection import CapabilityProjectionService
from src.inventory.access_model.capability_grants.tests import _seed_minimal_refs

_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_capability_grants_requires_at_least_one_filter(client) -> None:
    """GET /api/v0/capability-grants with no filters → 400."""
    response = await client.get('/api/v0/capability-grants')
    assert response.status_code == 400
    assert 'subject_id' in response.json()['detail']


@pytest.mark.asyncio
async def test_get_capability_grants_filters_by_subject_id_returns_seeded_rows(client, session_factory) -> None:
    """Seed one grant; GET filtered by subject_id → 200 with one item."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)
        service = CapabilityProjectionService(session)
        await service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=_NOW)
        await session.commit()

    response = await client.get(f'/api/v0/capability-grants?subject_id={refs.subject_id}')
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert str(data[0]['subject_id']) == str(refs.subject_id)
    assert data[0]['scope_value'] is None  # GLOBAL sentinel


@pytest.mark.asyncio
async def test_get_capability_grant_by_id_returns_404_when_missing(client) -> None:
    """GET /api/v0/capability-grants/99999 → 404."""
    response = await client.get('/api/v0/capability-grants/99999')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_capability_grant_by_id_returns_persisted_row(client, session_factory) -> None:
    """Seed one grant via service; GET by id → 200 with correct shape."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)
        service = CapabilityProjectionService(session)
        await service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=_NOW)
        await session.commit()

    # First fetch the list to get the id
    list_resp = await client.get(f'/api/v0/capability-grants?subject_id={refs.subject_id}')
    assert list_resp.status_code == 200
    grant_id = list_resp.json()[0]['id']

    get_resp = await client.get(f'/api/v0/capability-grants/{grant_id}')
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data['id'] == grant_id
    assert str(data['subject_id']) == str(refs.subject_id)
    assert str(data['capability_id']) == str(refs.capability_id)
    assert data['tombstoned_at'] is None


@pytest.mark.asyncio
async def test_get_capability_grants_active_only_excludes_tombstoned(client, session_factory) -> None:
    """Seed one active + one tombstoned grant; active_only=True (default) returns only active."""
    import sqlalchemy as sa
    from src.engines.access_effective.models import EffectiveGrant

    # Use a past date guaranteed to be before datetime.now(UTC) at test execution time.
    tombstone_ts = datetime(2024, 1, 1, tzinfo=UTC)

    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)
        service = CapabilityProjectionService(session)
        await service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=_NOW)

        # Tombstone EG then re-project to produce a tombstoned grant
        await session.execute(
            sa.update(EffectiveGrant)
            .where(EffectiveGrant.id == refs.effective_grant_id)
            .values(tombstoned_at=tombstone_ts)
        )
        await session.flush()

        from src.inventory.access_model.capability_grants.models import CapabilityGrant

        await session.execute(sa.update(CapabilityGrant).values(tombstoned_at=tombstone_ts))

        await session.commit()

    # active_only=True should return 0 rows
    response = await client.get(f'/api/v0/capability-grants?subject_id={refs.subject_id}&active_only=true')
    assert response.status_code == 200
    assert len(response.json()) == 0

    # active_only=False should return 1 row
    response_all = await client.get(f'/api/v0/capability-grants?subject_id={refs.subject_id}&active_only=false')
    assert response_all.status_code == 200
    assert len(response_all.json()) == 1
