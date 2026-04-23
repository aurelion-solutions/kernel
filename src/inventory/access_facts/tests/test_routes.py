# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessFact API routes — Step 13 current-state store shape."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.access_facts.routes import router as access_facts_router
from src.inventory.access_facts.service import AccessFactService

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


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

        ext = str(uuid.uuid4())
        resource = Resource(
            external_id=ext,
            application_id=app.id,
            kind='database',
            resource_type='database',
            resource_key=ext,
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
    action_slug: str = 'read',
    effect: AccessFactEffect = AccessFactEffect.allow,
    observed_at: datetime = _NOW,
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
            action_slug=action_slug,
            effect=effect,
            observed_at=observed_at,
        )
        await session.commit()
        return fact.id


@pytest.mark.asyncio
async def test_get_access_fact_response_shape(app_with_access_facts, engine) -> None:
    """GET /access-facts/{id} response has action_slug, is_active, revoked_at, observed_at; no action field."""
    ids = await _make_prerequisites(engine)
    fact_id = await _seed_access_fact(engine, ids['subject_id'], ids['resource_id'], 'administer')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-facts/{fact_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['id'] == str(fact_id)
    # New fields
    assert 'action_slug' in data
    assert data['action_slug'] == 'administer'
    assert 'is_active' in data
    assert data['is_active'] is True
    assert 'revoked_at' in data
    assert data['revoked_at'] is None
    assert 'observed_at' in data
    # Old field must be gone
    assert 'action' not in data


@pytest.mark.asyncio
async def test_list_access_facts_filter_action_slug(app_with_access_facts, engine) -> None:
    """GET /access-facts?action_slug=read returns only matching rows."""
    ids1 = await _make_prerequisites(engine)
    ids2 = await _make_prerequisites(engine)

    await _seed_access_fact(engine, ids1['subject_id'], ids1['resource_id'], 'read')
    await _seed_access_fact(engine, ids2['subject_id'], ids2['resource_id'], 'write')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-facts', params={'action_slug': 'read'})

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert all(r['action_slug'] == 'read' for r in data)


@pytest.mark.asyncio
async def test_list_access_facts_filter_is_active_true(app_with_access_facts, engine) -> None:
    """GET /access-facts?is_active=true returns only active rows."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    ids = await _make_prerequisites(engine)
    fact_id = await _seed_access_fact(engine, ids['subject_id'], ids['resource_id'], 'read')

    # Revoke the fact
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    svc = AccessFactService()
    async with sf() as session:
        await svc.revoke_fact(session, fact_id, observed_at=datetime(2026, 1, 2, tzinfo=UTC))
        await session.commit()

    # Seed another active fact
    ids2 = await _make_prerequisites(engine)
    await _seed_access_fact(engine, ids2['subject_id'], ids2['resource_id'], 'read')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-facts', params={'is_active': 'true'})

    assert response.status_code == 200
    data = response.json()
    assert all(r['is_active'] is True for r in data)


@pytest.mark.asyncio
async def test_list_access_facts_filter_is_active_false(app_with_access_facts, engine) -> None:
    """GET /access-facts?is_active=false returns only revoked rows."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    ids = await _make_prerequisites(engine)
    fact_id = await _seed_access_fact(engine, ids['subject_id'], ids['resource_id'], 'write')

    # Revoke the fact
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    svc = AccessFactService()
    async with sf() as session:
        await svc.revoke_fact(session, fact_id, observed_at=datetime(2026, 1, 2, tzinfo=UTC))
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            '/api/v0/access-facts',
            params={'subject_id': str(ids['subject_id']), 'is_active': 'false'},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert all(r['is_active'] is False for r in data)


@pytest.mark.asyncio
async def test_list_access_facts_unknown_action_slug_returns_empty(app_with_access_facts, engine) -> None:
    """GET /access-facts?action_slug=wat → [], status 200."""
    ids = await _make_prerequisites(engine)
    await _seed_access_fact(engine, ids['subject_id'], ids['resource_id'], 'read')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-facts', params={'action_slug': 'wat'})

    assert response.status_code == 200
    assert response.json() == []
