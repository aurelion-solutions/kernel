# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route-layer tests for single-row CRUD on org_units (Phase 20 M-A)."""

from typing import Any

from httpx import AsyncClient
import pytest
from src.inventory.org_units.schemas import OrgUnitBulkItem
from src.inventory.org_units.service import OrgUnitService


async def _seed_external(session_factory: Any, external_id: str, name: str) -> str:
    """Seed a single external org-unit and return its UUID as string."""
    svc = OrgUnitService()
    item = OrgUnitBulkItem(external_id=external_id, name=name, is_internal=False)
    async with session_factory() as session:
        rows = await svc.bulk_upsert_org_units(session, [item])
        await session.commit()
        return str(rows[0].id)


async def _seed_internal(session_factory: Any, external_id: str, name: str) -> str:
    """Seed a single internal org-unit and return its UUID as string."""
    svc = OrgUnitService()
    item = OrgUnitBulkItem(external_id=external_id, name=name, is_internal=True)
    async with session_factory() as session:
        rows = await svc.bulk_upsert_org_units(session, [item])
        await session.commit()
        return str(rows[0].id)


# ---------------------------------------------------------------------------
# POST /api/v0/org-units
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_org_unit_happy_path(client: AsyncClient) -> None:
    """POST with valid external payload returns 201 and body."""
    payload = {
        'external_id': 'rt-ext-1',
        'name': 'Route Vendor',
        'is_internal': False,
    }
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data['external_id'] == 'rt-ext-1'
    assert data['name'] == 'Route Vendor'
    assert data['is_internal'] is False
    assert data['description'] is None
    assert 'id' in data


@pytest.mark.asyncio
async def test_post_org_unit_with_description(client: AsyncClient) -> None:
    """POST with description populates the field in the response."""
    payload = {
        'external_id': 'rt-desc-1',
        'name': 'Described Corp',
        'description': 'A note',
        'is_internal': False,
    }
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 201
    assert resp.json()['description'] == 'A note'


@pytest.mark.asyncio
async def test_post_org_unit_is_internal_true_returns_422(client: AsyncClient) -> None:
    """POST with is_internal=true is rejected with 422 by Pydantic."""
    payload = {'external_id': 'rt-internal', 'name': 'Internal', 'is_internal': True}
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_org_unit_missing_is_internal_returns_422(client: AsyncClient) -> None:
    """POST without is_internal field is rejected with 422 (required field)."""
    payload = {'external_id': 'rt-no-flag', 'name': 'No Flag'}
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_org_unit_extra_field_returns_422(client: AsyncClient) -> None:
    """POST with extra unknown field is rejected with 422 (extra='forbid')."""
    payload = {
        'external_id': 'rt-extra',
        'name': 'Extra',
        'is_internal': False,
        'unknown_field': 'bad',
    }
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_org_unit_duplicate_external_id_returns_409(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """POST with duplicate external_id returns 409."""
    await _seed_external(session_factory, 'rt-dup-1', 'First')
    payload = {'external_id': 'rt-dup-1', 'name': 'Second', 'is_internal': False}
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_post_org_unit_internal_parent_returns_422(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """POST with parent_id pointing at an internal row returns 422."""
    int_id = await _seed_internal(session_factory, 'rt-int-parent', 'Internal Parent')
    payload = {
        'external_id': 'rt-bad-parent',
        'name': 'Bad Parent Child',
        'is_internal': False,
        'parent_id': int_id,
    }
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_org_unit_nonexistent_parent_returns_422(
    client: AsyncClient,
) -> None:
    """POST with parent_id pointing at a non-existent row returns 422."""
    import uuid  # noqa: PLC0415

    payload = {
        'external_id': 'rt-ghost-parent',
        'name': 'Ghost Parent Child',
        'is_internal': False,
        'parent_id': str(uuid.uuid4()),
    }
    resp = await client.post('/api/v0/org-units', json=payload)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v0/org-units/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_org_unit_hit(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """GET /{id} returns 200 with the org-unit body."""
    ou_id = await _seed_external(session_factory, 'rt-get-1', 'Get Me')
    resp = await client.get(f'/api/v0/org-units/{ou_id}')
    assert resp.status_code == 200
    data = resp.json()
    assert data['external_id'] == 'rt-get-1'
    assert 'description' in data


@pytest.mark.asyncio
async def test_get_org_unit_miss_returns_404(client: AsyncClient) -> None:
    """GET /{id} for non-existent id returns 404."""
    import uuid  # noqa: PLC0415

    resp = await client.get(f'/api/v0/org-units/{uuid.uuid4()}')
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/v0/org-units/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_org_unit_happy_path(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """PATCH happy path returns 200 with updated fields."""
    ou_id = await _seed_external(session_factory, 'rt-put-1', 'Old Name')
    resp = await client.patch(
        f'/api/v0/org-units/{ou_id}',
        json={'name': 'New Name', 'description': 'Added'},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['name'] == 'New Name'
    assert data['description'] == 'Added'


@pytest.mark.asyncio
async def test_patch_org_unit_extra_field_returns_422(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """PATCH with extra field (external_id) is rejected with 422."""
    ou_id = await _seed_external(session_factory, 'rt-put-extra', 'Put Extra')
    resp = await client.patch(
        f'/api/v0/org-units/{ou_id}',
        json={'name': 'New', 'external_id': 'hack'},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_org_unit_on_internal_row_returns_409(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """PATCH on an internal org-unit returns 409."""
    int_id = await _seed_internal(session_factory, 'rt-int-put', 'Internal Put')
    resp = await client.patch(f'/api/v0/org-units/{int_id}', json={'name': 'Renamed'})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_org_unit_missing_id_returns_404(client: AsyncClient) -> None:
    """PATCH on a non-existent id returns 404."""
    import uuid  # noqa: PLC0415

    resp = await client.patch(f'/api/v0/org-units/{uuid.uuid4()}', json={'name': 'Ghost'})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v0/org-units/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_org_unit_happy_path(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """DELETE returns 204 and subsequent GET returns 404."""
    ou_id = await _seed_external(session_factory, 'rt-del-1', 'Delete Me')
    resp = await client.delete(f'/api/v0/org-units/{ou_id}')
    assert resp.status_code == 204

    get_resp = await client.get(f'/api/v0/org-units/{ou_id}')
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_org_unit_on_internal_row_returns_409(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """DELETE on an internal org-unit returns 409."""
    int_id = await _seed_internal(session_factory, 'rt-int-del', 'Internal Del')
    resp = await client.delete(f'/api/v0/org-units/{int_id}')
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_org_unit_missing_id_returns_404(client: AsyncClient) -> None:
    """DELETE on a non-existent id returns 404."""
    import uuid  # noqa: PLC0415

    resp = await client.delete(f'/api/v0/org-units/{uuid.uuid4()}')
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# List endpoint still includes description field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_org_units_includes_description(
    client: AsyncClient,
    session_factory: Any,
) -> None:
    """GET /org-units list returns description field on each item."""
    from src.inventory.org_units.schemas import OrgUnitCreate  # noqa: PLC0415

    svc = OrgUnitService()
    data = OrgUnitCreate(
        external_id='rt-list-desc',
        name='List Desc Corp',
        description='List note',
        is_internal=False,
    )
    async with session_factory() as session:
        await svc.create_external_org_unit(session, data)
        await session.commit()

    resp = await client.get('/api/v0/org-units?limit=100&offset=0')
    assert resp.status_code == 200
    items = resp.json()['items']
    target = next((i for i in items if i['external_id'] == 'rt-list-desc'), None)
    assert target is not None
    assert target['description'] == 'List note'
