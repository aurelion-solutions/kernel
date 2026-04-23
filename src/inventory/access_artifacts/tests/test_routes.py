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
        artifact, _ = await svc.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type=kwargs.get('artifact_type', 'sap_role'),
            external_id=kwargs.get('external_id', f'ext-{uuid.uuid4().hex[:8]}'),
            payload=kwargs.get('payload', {'data': 'value'}),
            raw_name=kwargs.get('raw_name', None),
            effect=kwargs.get('effect', None),
            valid_from=kwargs.get('valid_from', None),
            valid_until=kwargs.get('valid_until', None),
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
async def test_get_access_artifacts_filter_by_artifact_type(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts?artifact_type= returns only matching artifacts."""
    app_id = await _make_application_id(engine)
    await _seed_artifact(engine, app_id, artifact_type='sap_role', external_id='role-001')
    await _seed_artifact(engine, app_id, artifact_type='acl_entry', external_id='acl-001')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-artifacts', params={'artifact_type': 'sap_role'})

    assert response.status_code == 200
    data = response.json()
    assert all(r['artifact_type'] == 'sap_role' for r in data)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_get_access_artifacts_old_source_kind_param_ignored(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts?source_kind= is silently ignored — returns all artifacts."""
    app_id = await _make_application_id(engine)
    await _seed_artifact(engine, app_id, artifact_type='sap_role', external_id='role-002')
    await _seed_artifact(engine, app_id, artifact_type='acl_entry', external_id='acl-002')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-artifacts', params={'source_kind': 'sap_role'})

    assert response.status_code == 200
    # Old param is unknown — FastAPI ignores it, both artifacts returned
    data = response.json()
    assert len(data) >= 2


@pytest.mark.asyncio
async def test_get_access_artifact_200_carries_new_fields(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts/{id} response carries artifact_type, observed_at, is_active, tombstoned_at."""
    app_id = await _make_application_id(engine)
    artifact_id = await _seed_artifact(engine, app_id, artifact_type='db_grant', external_id='grant-001')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-artifacts/{artifact_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['id'] == str(artifact_id)
    assert data['artifact_type'] == 'db_grant'
    assert 'source_kind' not in data
    assert data['external_id'] == 'grant-001'
    assert 'ingested_at' in data
    assert 'observed_at' in data
    assert data['is_active'] is True
    assert data['tombstoned_at'] is None


@pytest.mark.asyncio
async def test_get_access_artifacts_list_carries_new_fields(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts list response carries new fields on each item."""
    app_id = await _make_application_id(engine)
    await _seed_artifact(engine, app_id, artifact_type='sap_role', external_id='role-003')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-artifacts', params={'artifact_type': 'sap_role'})

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    item = data[0]
    assert 'artifact_type' in item
    assert 'source_kind' not in item
    assert 'observed_at' in item
    assert 'is_active' in item
    assert 'tombstoned_at' in item


@pytest.mark.asyncio
async def test_get_access_artifact_404(app_with_access_artifacts) -> None:
    """GET /access-artifacts/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-artifacts/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_access_artifact_by_id_returns_permitted_fields_set(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts/{id} returns the four permitted universal fields when set."""
    from datetime import UTC, datetime

    app_id = await _make_application_id(engine)
    artifact_id = await _seed_artifact(
        engine,
        app_id,
        artifact_type='sap_role',
        external_id='role-permitted-route-set',
        raw_name='SAP ADMIN Role',
        effect='grant',
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 12, 31, tzinfo=UTC),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-artifacts/{artifact_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['raw_name'] == 'SAP ADMIN Role'
    assert data['effect'] == 'grant'
    assert data['valid_from'] is not None
    assert data['valid_until'] is not None


@pytest.mark.asyncio
async def test_get_access_artifact_by_id_returns_permitted_fields_null(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts/{id} returns null for the four permitted universal fields when not set."""
    app_id = await _make_application_id(engine)
    artifact_id = await _seed_artifact(
        engine,
        app_id,
        artifact_type='acl_entry',
        external_id='acl-permitted-route-null',
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/access-artifacts/{artifact_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['raw_name'] is None
    assert data['effect'] is None
    assert data['valid_from'] is None
    assert data['valid_until'] is None


@pytest.mark.asyncio
async def test_get_access_artifacts_list_returns_permitted_fields_set(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts list returns the four permitted universal fields on each item when set."""
    from datetime import UTC, datetime

    app_id = await _make_application_id(engine)
    await _seed_artifact(
        engine,
        app_id,
        artifact_type='db_grant',
        external_id='grant-permitted-list-set',
        raw_name='DB SELECT Grant',
        effect='permit',
        valid_from=datetime(2026, 3, 1, tzinfo=UTC),
        valid_until=datetime(2026, 9, 30, tzinfo=UTC),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-artifacts', params={'artifact_type': 'db_grant'})

    assert response.status_code == 200
    items = [i for i in response.json() if i['external_id'] == 'grant-permitted-list-set']
    assert len(items) == 1
    item = items[0]
    assert item['raw_name'] == 'DB SELECT Grant'
    assert item['effect'] == 'permit'
    assert item['valid_from'] is not None
    assert item['valid_until'] is not None


@pytest.mark.asyncio
async def test_get_access_artifacts_list_returns_permitted_fields_null(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts list returns null for the four permitted fields when not set."""
    app_id = await _make_application_id(engine)
    await _seed_artifact(
        engine,
        app_id,
        artifact_type='sap_role',
        external_id='role-permitted-list-null',
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-artifacts', params={'artifact_type': 'sap_role'})

    assert response.status_code == 200
    items = [i for i in response.json() if i['external_id'] == 'role-permitted-list-null']
    assert len(items) == 1
    item = items[0]
    assert item['raw_name'] is None
    assert item['effect'] is None
    assert item['valid_from'] is None
    assert item['valid_until'] is None


@pytest.mark.asyncio
async def test_list_access_artifacts_is_active_filter(app_with_access_artifacts, engine) -> None:
    """GET /access-artifacts?is_active=true/false/unset filters by lifecycle status."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    app_id = await _make_application_id(engine)
    active_ext_id = f'active-filter-route-{uuid.uuid4().hex[:6]}'
    inactive_ext_id = f'inactive-filter-route-{uuid.uuid4().hex[:6]}'

    active_id = await _seed_artifact(
        engine,
        app_id,
        artifact_type='acl_entry',
        external_id=active_ext_id,
    )
    inactive_id = await _seed_artifact(
        engine,
        app_id,
        artifact_type='acl_entry',
        external_id=inactive_ext_id,
    )

    # Tombstone the inactive artifact directly via service
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    svc = AccessArtifactService()
    async with sf() as session:
        await svc.tombstone_artifact(session, artifact_id=inactive_id)
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_artifacts),
        base_url='http://testserver',
    ) as client:
        # is_active=true → only active
        resp_true = await client.get(
            '/api/v0/access-artifacts',
            params={'artifact_type': 'acl_entry', 'is_active': 'true'},
        )
        assert resp_true.status_code == 200
        ids_active = {r['id'] for r in resp_true.json()}
        assert str(active_id) in ids_active
        assert str(inactive_id) not in ids_active

        # is_active=false → only tombstoned
        resp_false = await client.get(
            '/api/v0/access-artifacts',
            params={'artifact_type': 'acl_entry', 'is_active': 'false'},
        )
        assert resp_false.status_code == 200
        ids_inactive = {r['id'] for r in resp_false.json()}
        assert str(inactive_id) in ids_inactive
        assert str(active_id) not in ids_inactive

        # no is_active param → both
        resp_all = await client.get(
            '/api/v0/access-artifacts',
            params={'artifact_type': 'acl_entry'},
        )
        assert resp_all.status_code == 200
        ids_all = {r['id'] for r in resp_all.json()}
        assert str(active_id) in ids_all
        assert str(inactive_id) in ids_all
