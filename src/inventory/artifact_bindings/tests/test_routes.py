# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ArtifactBinding API routes."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.inventory.artifact_bindings.routes import router as artifact_bindings_router


@pytest.fixture
def app_with_artifact_bindings(engine):
    """App with artifact binding routes using test engine."""
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
    app.include_router(artifact_bindings_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_prerequisites(engine) -> dict:
    """Create all required entities, return dict with ids."""
    from src.inventory.access_artifacts.models import AccessArtifact
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

        artifact = AccessArtifact(
            application_id=app.id,
            source_kind='acl_entry',
            external_id=str(uuid.uuid4()),
            payload={'raw': 'data'},
        )
        session.add(artifact)
        await session.flush()

        fact = AccessFact(
            subject_id=subj.id,
            resource_id=resource.id,
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        session.add(fact)
        await session.commit()

        return {
            'artifact_id': artifact.id,
            'access_fact_id': fact.id,
            'resource_id': resource.id,
        }


async def _seed_binding(
    engine,
    prereqs: dict,
    *,
    access_fact_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Create an ArtifactBinding directly via repository, return binding id."""
    from src.inventory.artifact_bindings.repository import create_artifact_binding

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        binding = await create_artifact_binding(
            session,
            artifact_id=prereqs['artifact_id'],
            access_fact_id=access_fact_id,
            resource_id=resource_id,
            account_id=account_id,
        )
        await session.commit()
        return binding.id


@pytest.mark.asyncio
async def test_get_artifact_bindings_200_empty(app_with_artifact_bindings) -> None:
    """GET /artifact-bindings returns 200 with empty list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/artifact-bindings')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_artifact_bindings_200_with_artifact_filter(app_with_artifact_bindings, engine) -> None:
    """GET /artifact-bindings with artifact_id filter returns only matching bindings."""
    prereqs1 = await _make_prerequisites(engine)
    prereqs2 = await _make_prerequisites(engine)

    binding1_id = await _seed_binding(engine, prereqs1, access_fact_id=prereqs1['access_fact_id'])
    await _seed_binding(engine, prereqs2, access_fact_id=prereqs2['access_fact_id'])

    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            '/api/v0/artifact-bindings',
            params={'artifact_id': str(prereqs1['artifact_id'])},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert all(r['artifact_id'] == str(prereqs1['artifact_id']) for r in data)
    assert any(r['id'] == str(binding1_id) for r in data)


@pytest.mark.asyncio
async def test_get_artifact_binding_200(app_with_artifact_bindings, engine) -> None:
    """GET /artifact-bindings/{id} returns 200 with correct data."""
    prereqs = await _make_prerequisites(engine)
    binding_id = await _seed_binding(engine, prereqs, access_fact_id=prereqs['access_fact_id'])

    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/artifact-bindings/{binding_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['id'] == str(binding_id)
    assert data['artifact_id'] == str(prereqs['artifact_id'])
    assert data['access_fact_id'] == str(prereqs['access_fact_id'])
    assert 'created_at' in data


@pytest.mark.asyncio
async def test_get_artifact_binding_404(app_with_artifact_bindings) -> None:
    """GET /artifact-bindings/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/artifact-bindings/{uuid.uuid4()}')
    assert response.status_code == 404
