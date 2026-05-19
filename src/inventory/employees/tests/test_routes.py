# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Employee API routes."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.employees.routes import router as employees_router
from src.inventory.org_units.repository import create_org_unit
from src.inventory.persons.repository import create_person
from src.inventory.subjects.routes import router as subjects_router


@pytest.fixture
def app_with_employees(engine):
    """App with employee routes using test engine."""
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
    app.include_router(employees_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture
async def person_id_for_employees(engine):
    """Create a person for employee tests."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
    async with session_factory() as session:
        person = await create_person(session, external_id='ext-emp', full_name='For employees')
        await session.commit()
        return person.id


@pytest.fixture
async def org_unit_id_for_employees(engine):
    """Create an org-unit for employee tests."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
    async with session_factory() as session:
        org_unit = await create_org_unit(
            session,
            external_id='ou-emp-test',
            name='Test Org',
            description=None,
            is_internal=False,
            parent_id=None,
        )
        await session.commit()
        return org_unit.id


@pytest.mark.asyncio
async def test_post_employees_returns_201(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """POST /employees with valid body returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
                'is_locked': False,
                'description': 'Alice',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['person_id'] == str(person_id_for_employees)
    assert data['is_locked'] is False
    assert data['description'] == 'Alice'


@pytest.mark.asyncio
async def test_post_employees_invalid_person_id_returns_404(
    app_with_employees,
) -> None:
    """POST /employees with invalid person_id returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(uuid.uuid4()),
            },
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_employees_returns_envelope(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """GET /employees?limit=10&offset=0 returns 200 with envelope."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
            },
        )
        response = await client.get('/api/v0/employees?limit=10&offset=0')
    assert response.status_code == 200
    data = response.json()
    assert 'items' in data
    assert 'total' in data
    assert 'limit' in data
    assert 'offset' in data
    assert isinstance(data['items'], list)
    assert len(data['items']) >= 1
    assert data['limit'] == 10
    assert data['offset'] == 0


@pytest.mark.asyncio
async def test_get_employees_missing_params_returns_422(
    app_with_employees,
) -> None:
    """GET /employees without params returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/employees')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_employees_missing_offset_returns_422(
    app_with_employees,
) -> None:
    """GET /employees?limit=10 without offset returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/employees?limit=10')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_employees_limit_zero_returns_422(
    app_with_employees,
) -> None:
    """GET /employees?limit=0&offset=0 returns 422 (ge=1)."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/employees?limit=0&offset=0')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_employees_limit_too_large_returns_422(
    app_with_employees,
) -> None:
    """GET /employees?limit=1001&offset=0 returns 422 (le=1000)."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/employees?limit=1001&offset=0')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_employees_past_the_end_returns_empty_items(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """GET /employees?limit=10&offset=9999 returns 200 with empty items and real total."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/employees',
            json={'person_id': str(person_id_for_employees)},
        )
        response = await client.get('/api/v0/employees?limit=10&offset=9999')
    assert response.status_code == 200
    data = response.json()
    assert data['items'] == []
    assert data['total'] >= 1
    assert data['limit'] == 10
    assert data['offset'] == 9999


@pytest.mark.asyncio
async def test_post_employees_with_org_unit_id_returns_org_unit_id(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
    org_unit_id_for_employees: uuid.UUID,
) -> None:
    """POST /employees with org_unit_id returns 201 with org_unit_id in response."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
                'org_unit_id': str(org_unit_id_for_employees),
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['org_unit_id'] == str(org_unit_id_for_employees)


@pytest.mark.asyncio
async def test_post_employees_without_org_unit_id_returns_none(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """POST /employees without org_unit_id returns 201 with org_unit_id=null."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employees',
            json={'person_id': str(person_id_for_employees)},
        )
    assert response.status_code == 201
    data = response.json()
    assert data['org_unit_id'] is None


@pytest.mark.asyncio
async def test_post_employees_invalid_org_unit_id_returns_404(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """POST /employees with unknown org_unit_id returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
                'org_unit_id': str(uuid.uuid4()),
            },
        )
    assert response.status_code == 404
    assert response.json()['detail'] == 'Org-unit not found'


@pytest.mark.asyncio
async def test_post_employees_description_round_trip(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """POST /employees with description returns the description in response."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
                'description': 'Senior Engineer',
            },
        )
    assert response.status_code == 201
    assert response.json()['description'] == 'Senior Engineer'


@pytest.mark.asyncio
async def test_get_employees_id_returns_org_unit_id_when_set(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
    org_unit_id_for_employees: uuid.UUID,
) -> None:
    """GET /employees/{id} returns org_unit_id in the response when set."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
                'org_unit_id': str(org_unit_id_for_employees),
            },
        )
    assert create_resp.status_code == 201
    employee_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/employees/{employee_id}')
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data['org_unit_id'] == str(org_unit_id_for_employees)


@pytest.mark.asyncio
async def test_get_employees_id_returns_200(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """GET /employees/{id} returns 200 when found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
            },
        )
    assert create_resp.status_code == 201
    employee_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/employees/{employee_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == employee_id


@pytest.mark.asyncio
async def test_get_employees_id_missing_returns_404(app_with_employees) -> None:
    """GET /employees/{id} returns 404 when not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/employees/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_employees_id_attributes_returns_200(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """GET /employees/{id}/attributes returns 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
            },
        )
    assert create_resp.status_code == 201
    employee_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/employees/{employee_id}/attributes',
            json={'key': 'dept', 'value': 'Eng'},
        )
        attrs_resp = await client.get(f'/api/v0/employees/{employee_id}/attributes')
    assert attrs_resp.status_code == 200
    attrs = attrs_resp.json()
    assert isinstance(attrs, list)
    assert len(attrs) >= 1


@pytest.mark.asyncio
async def test_get_employees_id_attributes_missing_employee_returns_404(
    app_with_employees,
) -> None:
    """GET /employees/{id}/attributes returns 404 when employee not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            f'/api/v0/employees/{uuid.uuid4()}/attributes',
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_employees_id_attributes_returns_201(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """POST /employees/{id}/attributes returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
            },
        )
    assert create_resp.status_code == 201
    employee_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        attr_resp = await client.post(
            f'/api/v0/employees/{employee_id}/attributes',
            json={'key': 'title', 'value': 'Engineer'},
        )
    assert attr_resp.status_code == 201
    data = attr_resp.json()
    assert data['key'] == 'title'
    assert data['value'] == 'Engineer'
    assert data['employee_id'] == employee_id


@pytest.mark.asyncio
async def test_post_employees_id_attributes_missing_employee_returns_404(
    app_with_employees,
) -> None:
    """POST /employees/{id}/attributes returns 404 when employee not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/employees/{uuid.uuid4()}/attributes',
            json={'key': 'title', 'value': 'Engineer'},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_employees_id_attributes_duplicate_key_returns_409(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """POST /employees/{id}/attributes with duplicate key returns 409."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
            },
        )
    assert create_resp.status_code == 201
    employee_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/employees/{employee_id}/attributes',
            json={'key': 'dupkey', 'value': 'v1'},
        )
        dup_resp = await client.post(
            f'/api/v0/employees/{employee_id}/attributes',
            json={'key': 'dupkey', 'value': 'v2'},
        )
    assert dup_resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_employees_id_attributes_key_returns_204(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """DELETE /employees/{id}/attributes/{key} returns 204."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
            },
        )
    assert create_resp.status_code == 201
    employee_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/employees/{employee_id}/attributes',
            json={'key': 'to_delete', 'value': 'x'},
        )
        del_resp = await client.delete(
            f'/api/v0/employees/{employee_id}/attributes/to_delete',
        )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_employees_id_attributes_key_missing_returns_404(
    app_with_employees,
    person_id_for_employees: uuid.UUID,
) -> None:
    """DELETE /employees/{id}/attributes/{key} returns 404 when attribute not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={
                'person_id': str(person_id_for_employees),
            },
        )
    assert create_resp.status_code == 201
    employee_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees),
        base_url='http://testserver',
    ) as client:
        response = await client.delete(
            f'/api/v0/employees/{employee_id}/attributes/nonexistent',
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Subject auto-creation integration test
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_employees_and_subjects(engine):
    """App with employee + subjects routes using test engine."""
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
    app.include_router(employees_router, prefix='/api/v0')
    app.include_router(subjects_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_post_employees_then_get_subjects_shows_new_row(
    app_with_employees_and_subjects,
    engine,
) -> None:
    """POST /employees then GET /subjects?kind=employee finds the auto-created Subject."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    sm = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as session:
        person = await create_person(session, external_id='ext-subj-api-emp', full_name='Subj API Emp')
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employees_and_subjects),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employees',
            json={'person_id': str(person.id)},
        )
        assert create_resp.status_code == 201
        employee_id = create_resp.json()['id']

        subj_resp = await client.get('/api/v0/subjects', params={'kind': 'employee'})
        assert subj_resp.status_code == 200

    subjects_data = subj_resp.json()
    matching = [s for s in subjects_data['items'] if s.get('principal_employee_id') == employee_id]
    assert len(matching) == 1
