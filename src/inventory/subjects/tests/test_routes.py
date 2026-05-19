# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Subject API routes."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.subjects.routes import router as subjects_router


@pytest.fixture
def app_with_subjects(engine):
    """App with subject routes using test engine."""
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(subjects_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_employee_id(engine) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
        await session.flush()
        emp = await create_employee(session, person_id=person.id)
        await session.commit()
        return emp.id


async def _make_nhi_id(engine) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.inventory.nhi.repository import create_nhi

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        nhi = await create_nhi(session, external_id=str(uuid.uuid4()), name='test-nhi', kind='service_account')
        await session.commit()
        return nhi.id


async def _make_customer_id(engine) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.inventory.customers.repository import create_customer

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        cust = await create_customer(session, external_id=str(uuid.uuid4()))
        await session.commit()
        return cust.id


@pytest.mark.asyncio
async def test_post_subjects_employee_returns_201(app_with_subjects, engine) -> None:
    """POST /subjects with employee kind returns 201."""
    emp_id = await _make_employee_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'route-emp-001',
                'kind': 'employee',
                'principal_employee_id': str(emp_id),
                'status': 'active',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['kind'] == 'employee'
    assert data['status'] == 'active'
    assert data['nhi_kind'] is None
    assert 'id' in data
    assert 'created_at' in data
    assert 'updated_at' in data


@pytest.mark.asyncio
async def test_post_subjects_nhi_returns_201(app_with_subjects, engine) -> None:
    """POST /subjects with nhi kind returns 201."""
    nhi_id = await _make_nhi_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'route-nhi-001',
                'kind': 'nhi',
                'nhi_kind': 'service_account',
                'principal_nhi_id': str(nhi_id),
                'status': 'active',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['kind'] == 'nhi'
    assert data['nhi_kind'] == 'service_account'


@pytest.mark.asyncio
async def test_post_subjects_customer_returns_201(app_with_subjects, engine) -> None:
    """POST /subjects with customer kind returns 201."""
    cust_id = await _make_customer_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'route-cust-001',
                'kind': 'customer',
                'principal_customer_id': str(cust_id),
                'status': 'registered',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['kind'] == 'customer'
    assert data['status'] == 'registered'


@pytest.mark.asyncio
async def test_get_subjects_returns_list(app_with_subjects, engine) -> None:
    """GET /subjects returns 200 with paginated envelope."""
    emp_id = await _make_employee_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'route-list-001',
                'kind': 'employee',
                'principal_employee_id': str(emp_id),
                'status': 'active',
            },
        )
        response = await client.get('/api/v0/subjects')
    assert response.status_code == 200
    data = response.json()
    assert 'items' in data
    assert 'total' in data
    assert 'limit' in data
    assert 'offset' in data
    assert isinstance(data['items'], list)
    assert data['total'] >= 1


@pytest.mark.asyncio
async def test_get_subjects_filter_by_kind(app_with_subjects, engine) -> None:
    """GET /subjects?kind=employee returns only employees in paginated envelope."""
    emp_id = await _make_employee_id(engine)
    cust_id = await _make_customer_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'flt-emp',
                'kind': 'employee',
                'principal_employee_id': str(emp_id),
                'status': 'active',
            },
        )
        await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'flt-cust',
                'kind': 'customer',
                'principal_customer_id': str(cust_id),
                'status': 'registered',
            },
        )
        response = await client.get('/api/v0/subjects?kind=employee')
    assert response.status_code == 200
    data = response.json()
    assert all(s['kind'] == 'employee' for s in data['items'])


@pytest.mark.asyncio
async def test_list_subjects_filter_by_principal_employee_id(app_with_subjects, engine) -> None:
    """GET /subjects?principal_employee_id=<id> returns only that subject."""
    emp_id = await _make_employee_id(engine)
    emp_id2 = await _make_employee_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        r1 = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'flt-peid-1',
                'kind': 'employee',
                'principal_employee_id': str(emp_id),
                'status': 'active',
            },
        )
        assert r1.status_code == 201
        await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'flt-peid-2',
                'kind': 'employee',
                'principal_employee_id': str(emp_id2),
                'status': 'active',
            },
        )
        response = await client.get(f'/api/v0/subjects?principal_employee_id={emp_id}')
    assert response.status_code == 200
    data = response.json()
    assert data['total'] == 1
    assert len(data['items']) == 1
    assert data['items'][0]['principal_employee_id'] == str(emp_id)


@pytest.mark.asyncio
async def test_list_subjects_pagination(app_with_subjects, engine) -> None:
    """GET /subjects?limit=1&offset=0 returns paginated results."""
    emp_id1 = await _make_employee_id(engine)
    emp_id2 = await _make_employee_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'pag-1',
                'kind': 'employee',
                'principal_employee_id': str(emp_id1),
                'status': 'active',
            },
        )
        await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'pag-2',
                'kind': 'employee',
                'principal_employee_id': str(emp_id2),
                'status': 'active',
            },
        )
        response = await client.get('/api/v0/subjects?limit=1&offset=0')
    assert response.status_code == 200
    data = response.json()
    assert data['limit'] == 1
    assert data['offset'] == 0
    assert len(data['items']) == 1
    assert data['total'] >= 2


@pytest.mark.asyncio
async def test_list_subjects_response_envelope(app_with_subjects, engine) -> None:
    """GET /subjects returns SubjectListResponse envelope fields."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/subjects?limit=50&offset=0')
    assert response.status_code == 200
    data = response.json()
    assert 'items' in data
    assert 'total' in data
    assert data['limit'] == 50
    assert data['offset'] == 0


@pytest.mark.asyncio
async def test_get_subject_by_id_returns_200(app_with_subjects, engine) -> None:
    """GET /subjects/{id} returns 200 when found."""
    emp_id = await _make_employee_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'route-get-001',
                'kind': 'employee',
                'principal_employee_id': str(emp_id),
                'status': 'active',
            },
        )
    assert create_resp.status_code == 201
    subject_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/subjects/{subject_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == subject_id


@pytest.mark.asyncio
async def test_get_subject_missing_returns_404(app_with_subjects) -> None:
    """GET /subjects/{id} returns 404 when not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/subjects/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_subject_status(app_with_subjects, engine) -> None:
    """PATCH /subjects/{id} updates status."""
    emp_id = await _make_employee_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'route-patch-001',
                'kind': 'employee',
                'principal_employee_id': str(emp_id),
                'status': 'hired',
            },
        )
    assert create_resp.status_code == 201
    subject_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        patch_resp = await client.patch(
            f'/api/v0/subjects/{subject_id}',
            json={'status': 'active'},
        )
    assert patch_resp.status_code == 200
    assert patch_resp.json()['status'] == 'active'


@pytest.mark.asyncio
async def test_patch_subject_invalid_status_for_kind_returns_422(app_with_subjects, engine) -> None:
    """PATCH /subjects/{id} with wrong status for kind returns 422."""
    emp_id = await _make_employee_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'route-invalstatus',
                'kind': 'employee',
                'principal_employee_id': str(emp_id),
                'status': 'active',
            },
        )
    subject_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        patch_resp = await client.patch(
            f'/api/v0/subjects/{subject_id}',
            json={'status': 'expired'},  # NHI status, invalid for employee
        )
    assert patch_resp.status_code == 422


@pytest.mark.asyncio
async def test_post_subject_duplicate_principal_returns_409(app_with_subjects, engine) -> None:
    """POST /subjects with same principal twice returns 409."""
    emp_id = await _make_employee_id(engine)
    payload = {
        'external_id': 'dup-emp',
        'kind': 'employee',
        'principal_employee_id': str(emp_id),
        'status': 'active',
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        r1 = await client.post('/api/v0/subjects', json=payload)
        assert r1.status_code == 201
        r2 = await client.post('/api/v0/subjects', json={**payload, 'external_id': 'dup-emp-2'})
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_post_subject_nonexistent_principal_returns_422(app_with_subjects) -> None:
    """POST /subjects with nonexistent principal_employee_id returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/subjects',
            json={
                'external_id': 'ghost-emp',
                'kind': 'employee',
                'principal_employee_id': str(uuid.uuid4()),
                'status': 'active',
            },
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# SubjectAttribute route tests
# ---------------------------------------------------------------------------


async def _create_customer_subject(engine) -> uuid.UUID:
    """Create a customer + customer subject, return the subject id."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.inventory.customers.repository import create_customer
    from src.inventory.subjects.models import SubjectKind
    from src.inventory.subjects.repository import create_subject

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        cust = await create_customer(session, external_id=str(uuid.uuid4()))
        await session.flush()
        subj = await create_subject(
            session,
            external_id=str(uuid.uuid4()),
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()
        return subj.id


@pytest.mark.asyncio
async def test_list_subject_attributes_empty_returns_200(app_with_subjects, engine) -> None:
    """GET /subjects/{id}/attributes returns 200 and empty list."""
    subj_id = await _create_customer_subject(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/subjects/{subj_id}/attributes')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_add_subject_attribute_201_and_round_trip_via_get(app_with_subjects, engine) -> None:
    """POST /subjects/{id}/attributes returns 201; GET returns the attribute."""
    subj_id = await _create_customer_subject(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        post_resp = await client.post(
            f'/api/v0/subjects/{subj_id}/attributes',
            json={'key': 'legal_entity', 'value': 'ACME Corp'},
        )
        assert post_resp.status_code == 201
        data = post_resp.json()
        assert data['key'] == 'legal_entity'
        assert data['value'] == 'ACME Corp'
        assert data['subject_id'] == str(subj_id)

        get_resp = await client.get(f'/api/v0/subjects/{subj_id}/attributes')
        assert get_resp.status_code == 200
        attrs = get_resp.json()
        assert len(attrs) == 1
        assert attrs[0]['key'] == 'legal_entity'


@pytest.mark.asyncio
async def test_add_subject_attribute_duplicate_returns_409(app_with_subjects, engine) -> None:
    """POST /subjects/{id}/attributes with duplicate key returns 409."""
    subj_id = await _create_customer_subject(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        r1 = await client.post(
            f'/api/v0/subjects/{subj_id}/attributes',
            json={'key': 'dup_key', 'value': 'v1'},
        )
        assert r1.status_code == 201
        r2 = await client.post(
            f'/api/v0/subjects/{subj_id}/attributes',
            json={'key': 'dup_key', 'value': 'v2'},
        )
        assert r2.status_code == 409


@pytest.mark.asyncio
async def test_add_subject_attribute_unknown_subject_returns_404(app_with_subjects) -> None:
    """POST /subjects/{id}/attributes for unknown subject returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/subjects/{uuid.uuid4()}/attributes',
            json={'key': 'k', 'value': 'v'},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_remove_subject_attribute_204_then_404_on_second_delete(app_with_subjects, engine) -> None:
    """DELETE /subjects/{id}/attributes/{key} returns 204; second DELETE returns 404."""
    subj_id = await _create_customer_subject(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/subjects/{subj_id}/attributes',
            json={'key': 'rm_key', 'value': 'v'},
        )
        r1 = await client.delete(f'/api/v0/subjects/{subj_id}/attributes/rm_key')
        assert r1.status_code == 204
        r2 = await client.delete(f'/api/v0/subjects/{subj_id}/attributes/rm_key')
        assert r2.status_code == 404
