# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for OwnershipAssignment API routes."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.inventory.ownership_assignments.routes import router as ownership_assignments_router


@pytest.fixture
def app_with_ownership_assignments(engine):
    """App with ownership assignment routes using test engine."""
    from fastapi import FastAPI

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
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(ownership_assignments_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_subject(engine) -> uuid.UUID:
    """Create a minimal subject, return subject.id."""
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
        await session.flush()
        emp = await create_employee(session, person_id=person.id)
        await session.flush()
        subj = Subject(
            external_id=str(uuid.uuid4()),
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        session.add(subj)
        await session.commit()
        return subj.id


async def _make_resource(engine) -> uuid.UUID:
    """Create a minimal resource, return resource.id."""
    from src.inventory.resources.models import Resource
    from src.platform.applications.models import Application

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        app = Application(
            name=f'test-app-{uuid.uuid4()}',
            code=f'app-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        resource = Resource(
            external_id=str(uuid.uuid4()),
            application_id=app.id,
            kind='database',
        )
        session.add(resource)
        await session.commit()
        return resource.id


@pytest.mark.asyncio
async def test_list_ownership_assignments_200_empty(app_with_ownership_assignments) -> None:
    """GET /ownership-assignments returns 200 with empty list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_ownership_assignments),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/ownership-assignments')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_ownership_assignments_filter_by_subject(app_with_ownership_assignments, engine) -> None:
    """GET /ownership-assignments?subject_id=... returns only matching assignments."""
    subject_id1 = await _make_subject(engine)
    subject_id2 = await _make_subject(engine)
    resource_id1 = await _make_resource(engine)
    resource_id2 = await _make_resource(engine)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_ownership_assignments),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/ownership-assignments',
            json={
                'subject_id': str(subject_id1),
                'resource_id': str(resource_id1),
                'kind': 'primary',
            },
        )
        await client.post(
            '/api/v0/ownership-assignments',
            json={
                'subject_id': str(subject_id2),
                'resource_id': str(resource_id2),
                'kind': 'primary',
            },
        )
        response = await client.get(
            '/api/v0/ownership-assignments',
            params={'subject_id': str(subject_id1)},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert all(r['subject_id'] == str(subject_id1) for r in data)


@pytest.mark.asyncio
async def test_post_ownership_assignment_201(app_with_ownership_assignments, engine) -> None:
    """POST /ownership-assignments returns 201 with correct body."""
    subject_id = await _make_subject(engine)
    resource_id = await _make_resource(engine)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_ownership_assignments),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/ownership-assignments',
            json={
                'subject_id': str(subject_id),
                'resource_id': str(resource_id),
                'kind': 'primary',
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['subject_id'] == str(subject_id)
    assert data['resource_id'] == str(resource_id)
    assert data['account_id'] is None
    assert data['kind'] == 'primary'
    assert 'created_at' in data


@pytest.mark.asyncio
async def test_post_ownership_assignment_422_both_targets(app_with_ownership_assignments, engine) -> None:
    """POST /ownership-assignments with both resource_id and account_id returns 422."""
    subject_id = await _make_subject(engine)
    resource_id = await _make_resource(engine)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_ownership_assignments),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/ownership-assignments',
            json={
                'subject_id': str(subject_id),
                'resource_id': str(resource_id),
                'account_id': str(uuid.uuid4()),
                'kind': 'primary',
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_ownership_assignment_409_duplicate(app_with_ownership_assignments, engine) -> None:
    """POST the same assignment twice returns 409 on the second call."""
    subject_id = await _make_subject(engine)
    resource_id = await _make_resource(engine)
    payload = {
        'subject_id': str(subject_id),
        'resource_id': str(resource_id),
        'kind': 'primary',
    }

    async with AsyncClient(
        transport=ASGITransport(app=app_with_ownership_assignments),
        base_url='http://testserver',
    ) as client:
        r1 = await client.post('/api/v0/ownership-assignments', json=payload)
        assert r1.status_code == 201
        r2 = await client.post('/api/v0/ownership-assignments', json=payload)
        assert r2.status_code == 409


@pytest.mark.asyncio
async def test_delete_ownership_assignment_204(app_with_ownership_assignments, engine) -> None:
    """POST then DELETE returns 204; subsequent GET returns 404."""
    subject_id = await _make_subject(engine)
    resource_id = await _make_resource(engine)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_ownership_assignments),
        base_url='http://testserver',
    ) as client:
        r_create = await client.post(
            '/api/v0/ownership-assignments',
            json={
                'subject_id': str(subject_id),
                'resource_id': str(resource_id),
                'kind': 'secondary',
            },
        )
        assert r_create.status_code == 201
        assignment_id = r_create.json()['id']

        r_delete = await client.delete(f'/api/v0/ownership-assignments/{assignment_id}')
        assert r_delete.status_code == 204

        r_get = await client.get(f'/api/v0/ownership-assignments/{assignment_id}')
        assert r_get.status_code == 404
