# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessFact API routes."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.access_facts.routes import router as access_facts_router
from src.inventory.access_facts.service import AccessFactService
from src.inventory.enums import Action


@pytest.fixture
def app_with_access_facts(engine):
    """App with access fact routes using test engine."""
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
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(access_facts_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_prerequisites(engine) -> dict:
    """Create employee, subject, resource. Return dict with ids."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.inventory.employees.repository import create_employee
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
        await session.commit()

        return {
            'subject_id': subj.id,
            'resource_id': resource.id,
        }


async def _seed_access_fact(
    engine,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
    action: Action = Action.read,
    effect: AccessFactEffect = AccessFactEffect.allow,
) -> uuid.UUID:
    """Seed a fact via service and return its id."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    svc = AccessFactService()
    async with sf() as session:
        fact = await svc.create_fact(
            session,
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action=action,
            effect=effect,
        )
        await session.commit()
        return fact.id


@pytest.mark.asyncio
async def test_get_access_facts_200_empty(app_with_access_facts) -> None:
    """GET /access-facts returns 200 with empty list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-facts')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_access_facts_200_with_subject_filter(app_with_access_facts, engine) -> None:
    """GET /access-facts with subject_id filter returns only matching facts."""
    ids1 = await _make_prerequisites(engine)
    ids2 = await _make_prerequisites(engine)

    await _seed_access_fact(engine, ids1['subject_id'], ids1['resource_id'], Action.read)
    await _seed_access_fact(engine, ids2['subject_id'], ids2['resource_id'], Action.write)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            '/api/v0/access-facts',
            params={'subject_id': str(ids1['subject_id'])},
        )

    assert response.status_code == 200
    data = response.json()
    assert all(r['subject_id'] == str(ids1['subject_id']) for r in data)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_get_access_fact_200(app_with_access_facts, engine) -> None:
    """GET /access-facts/{id} returns 200 with correct data."""
    ids = await _make_prerequisites(engine)
    fact_id = await _seed_access_fact(engine, ids['subject_id'], ids['resource_id'], Action.administer)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-facts/{fact_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['id'] == str(fact_id)
    assert data['subject_id'] == str(ids['subject_id'])
    assert data['resource_id'] == str(ids['resource_id'])
    assert data['action'] == 'administer'
    assert data['effect'] == 'allow'
    assert 'valid_from' in data
    assert 'created_at' in data


@pytest.mark.asyncio
async def test_get_access_fact_404(app_with_access_facts) -> None:
    """GET /access-facts/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-facts/{uuid.uuid4()}')
    assert response.status_code == 404
