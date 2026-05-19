# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for NHI API routes."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.employees.models import Employee
from src.inventory.nhi.routes import router as nhi_router
from src.inventory.persons.models import Person
from src.inventory.subjects.routes import router as subjects_router
from src.platform.applications.models import Application


@pytest.fixture
def app_with_nhi(engine):
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
    app.include_router(nhi_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture
async def person_employee_app_ids(engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
    async with session_factory() as session:
        person = Person(external_id='rt-p', full_name='P')
        session.add(person)
        await session.flush()
        employee = Employee(person_id=person.id, is_locked=False)
        session.add(employee)
        await session.flush()
        app = Application(name='rt-app', code='rt-app', config={})
        session.add(app)
        await session.commit()
        return employee.id, app.id


@pytest.mark.asyncio
async def test_post_nhi_returns_201(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-post',
                'name': 'Test',
                'kind': 'bot',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['external_id'] == 'nhi-post'
    assert data['name'] == 'Test'


@pytest.mark.asyncio
async def test_post_nhi_invalid_owner_returns_404(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-bad',
                'name': 'X',
                'kind': 'bot',
                'owner_employee_id': str(uuid.uuid4()),
            },
        )
    assert response.status_code == 404
    assert 'Employee' in response.json()['detail']


@pytest.mark.asyncio
async def test_post_nhi_invalid_application_returns_404(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-bad-app',
                'name': 'X',
                'kind': 'bot',
                'application_id': str(uuid.uuid4()),
            },
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_nhi_list(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-list',
                'name': 'L',
                'kind': 'bot',
            },
        )
        response = await client.get('/api/v0/nhi')
    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_get_nhi_by_id(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-get',
                'name': 'G',
                'kind': 'bot',
            },
        )
    assert create_resp.status_code == 201
    nid = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/nhi/{nid}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == nid


@pytest.mark.asyncio
async def test_get_nhi_missing_returns_404(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/nhi/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_attributes_flow(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-attr',
                'name': 'A',
                'kind': 'bot',
            },
        )
    nid = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/nhi/{nid}/attributes',
            json={'key': 'k1', 'value': 'v1'},
        )
        attrs_resp = await client.get(f'/api/v0/nhi/{nid}/attributes')
    assert attrs_resp.status_code == 200
    assert len(attrs_resp.json()) >= 1


@pytest.mark.asyncio
async def test_post_attribute_duplicate_returns_409(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-dup',
                'name': 'D',
                'kind': 'bot',
            },
        )
    nid = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/nhi/{nid}/attributes',
            json={'key': 'dup', 'value': 'a'},
        )
        dup_resp = await client.post(
            f'/api/v0/nhi/{nid}/attributes',
            json={'key': 'dup', 'value': 'b'},
        )
    assert dup_resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_attribute_returns_204(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-del',
                'name': 'D',
                'kind': 'bot',
            },
        )
    nid = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/nhi/{nid}/attributes',
            json={'key': 'todel', 'value': 'x'},
        )
        del_resp = await client.delete(
            f'/api/v0/nhi/{nid}/attributes/todel',
        )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_attributes_missing_nhi_returns_404(app_with_nhi) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            f'/api/v0/nhi/{uuid.uuid4()}/attributes',
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_nhi_with_fks(
    app_with_nhi,
    person_employee_app_ids: tuple[uuid.UUID, uuid.UUID],
) -> None:
    emp_id, app_id = person_employee_app_ids
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-fk',
                'name': 'F',
                'kind': 'service_account',
                'owner_employee_id': str(emp_id),
                'application_id': str(app_id),
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['owner_employee_id'] == str(emp_id)
    assert data['application_id'] == str(app_id)


# ---------------------------------------------------------------------------
# Subject auto-creation integration test
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_nhi_and_subjects(engine):
    """App with NHI + subjects routes using test engine."""
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
    app.include_router(nhi_router, prefix='/api/v0')
    app.include_router(subjects_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_post_nhis_then_get_subjects_shows_new_row(
    app_with_nhi_and_subjects,
) -> None:
    """POST /nhi then GET /subjects?kind=nhi finds the auto-created Subject."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_nhi_and_subjects),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/nhi',
            json={
                'external_id': 'nhi-subj-api-test',
                'name': 'NHI Subj API',
                'kind': 'service_account',
            },
        )
        assert create_resp.status_code == 201
        nhi_id = create_resp.json()['id']

        subj_resp = await client.get('/api/v0/subjects', params={'kind': 'nhi'})
        assert subj_resp.status_code == 200

    subjects_data = subj_resp.json()
    matching = [s for s in subjects_data['items'] if s.get('principal_nhi_id') == nhi_id]
    assert len(matching) == 1
