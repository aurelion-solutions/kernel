# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke tests for POST /org-units/bulk (lake-first path) and GET /org-units."""

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.org_units.routes import router as org_units_router
from src.inventory.org_units.service import OrgUnitService


@pytest.fixture
def app_no_lake() -> FastAPI:
    app = FastAPI()
    app.include_router(org_units_router)
    return app


@pytest.mark.asyncio
async def test_bulk_org_units_no_lake_returns_503(app_no_lake: FastAPI) -> None:
    """Without lake_catalog in app.state the endpoint returns 503."""
    payload = {'items': [{'external_id': 'OU1', 'name': 'Engineering'}]}
    async with AsyncClient(transport=ASGITransport(app=app_no_lake), base_url='http://test') as client:
        resp = await client.post('/org-units/bulk', json=payload)
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_bulk_accepts_is_internal_without_422(app_no_lake: FastAPI) -> None:
    """POST /bulk accepts is_internal in items without raising a 422 validation error.

    The field passes Pydantic validation; the 503 comes from missing lake
    backend (not from schema rejection), which confirms the field is valid.
    """
    payload = {
        'items': [
            {'external_id': 'OU-INT', 'name': 'Internal', 'is_internal': True},
            {'external_id': 'OU-EXT', 'name': 'External', 'is_internal': False},
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app_no_lake), base_url='http://test') as client:
        resp = await client.post('/org-units/bulk', json=payload)
    # 503 means the payload was valid (lake is just not configured in app_no_lake).
    assert resp.status_code != 422


@pytest.mark.asyncio
async def test_get_org_units_includes_is_internal(
    session_factory: Any,
    client: Any,
) -> None:
    """GET /api/v0/org-units returns is_internal in each item and pagination envelope."""
    svc = OrgUnitService()
    from src.inventory.org_units.schemas import OrgUnitBulkItem  # noqa: PLC0415

    items = [
        OrgUnitBulkItem(external_id='get-int-1', name='Internal Unit', is_internal=True),
        OrgUnitBulkItem(external_id='get-ext-1', name='External Unit', is_internal=False),
    ]
    async with session_factory() as session:
        await svc.bulk_upsert_org_units(session, items)
        await session.commit()

    resp = await client.get('/api/v0/org-units?limit=10&offset=0')
    assert resp.status_code == 200
    data = resp.json()
    assert 'items' in data
    assert 'total' in data
    assert 'limit' in data
    assert 'offset' in data
    assert data['total'] == 2
    assert data['limit'] == 10
    assert data['offset'] == 0

    by_ext_id = {item['external_id']: item for item in data['items']}
    assert 'get-int-1' in by_ext_id
    assert 'get-ext-1' in by_ext_id
    assert by_ext_id['get-int-1']['is_internal'] is True
    assert by_ext_id['get-ext-1']['is_internal'] is False


@pytest.mark.asyncio
async def test_get_org_units_pagination_limit_offset(
    session_factory: Any,
    client: Any,
) -> None:
    """Paginated GET /api/v0/org-units walks through 5 rows correctly."""
    svc = OrgUnitService()
    from src.inventory.org_units.schemas import OrgUnitBulkItem  # noqa: PLC0415

    seed_items = [OrgUnitBulkItem(external_id=f'OU-0{i}', name=f'Unit {i}', is_internal=True) for i in range(1, 6)]
    async with session_factory() as session:
        await svc.bulk_upsert_org_units(session, seed_items)
        await session.commit()

    # Page 1: items OU-01, OU-02
    resp = await client.get('/api/v0/org-units?limit=2&offset=0')
    assert resp.status_code == 200
    data = resp.json()
    assert len(data['items']) == 2
    assert data['items'][0]['external_id'] == 'OU-01'
    assert data['items'][1]['external_id'] == 'OU-02'
    assert data['total'] == 5
    assert data['limit'] == 2
    assert data['offset'] == 0

    # Page 2: items OU-03, OU-04
    resp = await client.get('/api/v0/org-units?limit=2&offset=2')
    assert resp.status_code == 200
    data = resp.json()
    assert [it['external_id'] for it in data['items']] == ['OU-03', 'OU-04']
    assert data['total'] == 5
    assert data['offset'] == 2

    # Page 3: only OU-05
    resp = await client.get('/api/v0/org-units?limit=2&offset=4')
    assert resp.status_code == 200
    data = resp.json()
    assert [it['external_id'] for it in data['items']] == ['OU-05']
    assert data['total'] == 5

    # Past-the-end: empty items, total still 5
    resp = await client.get('/api/v0/org-units?limit=2&offset=100')
    assert resp.status_code == 200
    data = resp.json()
    assert data['items'] == []
    assert data['total'] == 5


@pytest.mark.asyncio
async def test_get_org_units_parent_id_in_list_response(
    session_factory: Any,
    client: Any,
) -> None:
    """GET /api/v0/org-units returns parent_id on every item.

    Root org-unit has parent_id null; child has parent_id equal to the root's id.
    """
    svc = OrgUnitService()
    from src.inventory.org_units.schemas import OrgUnitBulkItem  # noqa: PLC0415

    items = [
        OrgUnitBulkItem(external_id='pid-root', name='Root Unit', is_internal=True),
        OrgUnitBulkItem(external_id='pid-child', name='Child Unit', is_internal=True, parent_external_id='pid-root'),
    ]
    async with session_factory() as session:
        await svc.bulk_upsert_org_units(session, items)
        await session.commit()

    resp = await client.get('/api/v0/org-units?limit=10&offset=0')
    assert resp.status_code == 200
    data = resp.json()

    by_ext_id = {item['external_id']: item for item in data['items']}
    assert 'pid-root' in by_ext_id
    assert 'pid-child' in by_ext_id

    root_item = by_ext_id['pid-root']
    child_item = by_ext_id['pid-child']

    assert 'parent_id' in root_item
    assert 'parent_id' in child_item
    assert root_item['parent_id'] is None
    assert child_item['parent_id'] == root_item['id']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [
        # Missing params — both required
        '',
        'limit=2',
        'offset=0',
        # Out-of-range values
        'limit=0&offset=0',
        'limit=1001&offset=0',
        'limit=2&offset=-1',
        'limit=abc&offset=0',
    ],
)
async def test_get_org_units_requires_pagination(
    app_no_lake: FastAPI,
    params: str,
) -> None:
    """Missing or invalid limit/offset produce a 422 from FastAPI input validation.

    Both limit and offset are required; omitting either is a contract error.
    """
    url = f'/org-units?{params}' if params else '/org-units'
    async with AsyncClient(transport=ASGITransport(app=app_no_lake), base_url='http://test') as c:
        resp = await c.get(url)
    assert resp.status_code == 422
