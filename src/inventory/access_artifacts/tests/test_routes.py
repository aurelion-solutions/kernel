# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessArtifact API routes."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.access_artifacts.routes import router as access_artifacts_router
from src.inventory.access_artifacts.service import AccessArtifactService


@pytest.fixture
def app_with_access_artifacts(engine):
    """App with access artifact routes using test engine."""
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
    app.include_router(access_artifacts_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_application_id(engine) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
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
        await session.commit()
        return app.id


async def _seed_artifact(engine, app_id: uuid.UUID, **kwargs) -> uuid.UUID:
    """Seed an artifact via service and return its id."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    svc = AccessArtifactService()
    async with sf() as session:
        artifact = await svc.create_artifact(
            session,
            application_id=app_id,
            source_kind=kwargs.get('source_kind', 'sap_role'),
            external_id=kwargs.get('external_id', f'ext-{uuid.uuid4().hex[:8]}'),
            payload=kwargs.get('payload', {'data': 'value'}),
        )
        await session.commit()
        return artifact.id


@pytest.mark.asyncio
async def test_get_access_artifacts_200_empty(app_with_access_artifacts) -> None:
    """GET /access-artifacts returns 200 with empty list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-artifacts')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_access_artifacts_200_with_filters(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts with source_kind filter returns only matching artifacts."""
    app_id = await _make_application_id(engine)
    await _seed_artifact(engine, app_id, source_kind='sap_role', external_id='role-001')
    await _seed_artifact(engine, app_id, source_kind='acl_entry', external_id='acl-001')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-artifacts', params={'source_kind': 'sap_role'})

    assert response.status_code == 200
    data = response.json()
    assert all(r['source_kind'] == 'sap_role' for r in data)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_get_access_artifact_200(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts/{id} returns 200 with correct data."""
    app_id = await _make_application_id(engine)
    artifact_id = await _seed_artifact(engine, app_id, source_kind='db_grant', external_id='grant-001')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-artifacts/{artifact_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['id'] == str(artifact_id)
    assert data['source_kind'] == 'db_grant'
    assert data['external_id'] == 'grant-001'
    assert 'ingested_at' in data


@pytest.mark.asyncio
async def test_get_access_artifact_404(app_with_access_artifacts) -> None:
    """GET /access-artifacts/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-artifacts/{uuid.uuid4()}')
    assert response.status_code == 404
