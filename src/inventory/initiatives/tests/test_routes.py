# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Initiative API routes."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.inventory.initiatives.routes import router as initiatives_router


@pytest.fixture
def app_with_initiatives(engine):
    """App with initiative routes using test engine."""
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
    app.include_router(initiatives_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_access_fact(engine) -> uuid.UUID:
    """Create minimal prerequisites and return an access_fact id."""
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.employees.repository import create_employee
    from src.inventory.enums import Action
    from src.inventory.persons.repository import create_person
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind
    from src.platform.applications.models import Application

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
        await session.flush()
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
        await session.flush()
        fact = AccessFact(
            subject_id=subj.id,
            resource_id=resource.id,
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        session.add(fact)
        await session.commit()
        return fact.id


async def _seed_initiative(
    engine,
    fact_id: uuid.UUID,
    *,
    type_: str = 'birthright',
    origin: str = 'seeded',
) -> uuid.UUID:
    """Create an initiative directly via repository, return initiative id."""
    from src.inventory.initiatives.models import InitiativeType
    from src.inventory.initiatives.repository import create_initiative

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        initiative = await create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType(type_),
            origin=origin,
        )
        await session.commit()
        return initiative.id


@pytest.mark.asyncio
async def test_list_initiatives_200_empty(app_with_initiatives) -> None:
    """GET /initiatives returns 200 with empty list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_initiatives),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/initiatives')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_initiatives_200_with_type_filter(app_with_initiatives, engine) -> None:
    """GET /initiatives?type=birthright returns only matching initiatives."""
    fact_id = await _make_access_fact(engine)
    await _seed_initiative(engine, fact_id, type_='birthright', origin='auto')
    await _seed_initiative(engine, fact_id, type_='requested', origin='manual request')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_initiatives),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/initiatives', params={'type': 'birthright'})

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert all(r['type'] == 'birthright' for r in data)


@pytest.mark.asyncio
async def test_post_initiative_201(app_with_initiatives, engine) -> None:
    """POST /initiatives returns 201 and the created initiative."""
    fact_id = await _make_access_fact(engine)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_initiatives),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/initiatives',
            json={
                'access_fact_id': str(fact_id),
                'type': 'delegated',
                'origin': 'Delegated by manager',
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['access_fact_id'] == str(fact_id)
    assert data['type'] == 'delegated'
    assert data['origin'] == 'Delegated by manager'


@pytest.mark.asyncio
async def test_post_initiative_422_bad_access_fact(app_with_initiatives) -> None:
    """POST /initiatives with unknown access_fact_id returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_initiatives),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/initiatives',
            json={
                'access_fact_id': str(uuid.uuid4()),
                'type': 'invited',
                'origin': 'Should fail',
            },
        )

    assert response.status_code == 422
    assert 'Access fact not found' in response.json()['detail']


@pytest.mark.asyncio
async def test_patch_initiative_200_updates_origin(app_with_initiatives, engine) -> None:
    """PATCH /initiatives/{id} updates origin and returns 200."""
    fact_id = await _make_access_fact(engine)
    initiative_id = await _seed_initiative(engine, fact_id, origin='old origin')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_initiatives),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/initiatives/{initiative_id}',
            json={'origin': 'new origin'},
        )

    assert response.status_code == 200
    data = response.json()
    assert data['origin'] == 'new origin'
    assert data['id'] == str(initiative_id)
