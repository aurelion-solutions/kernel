# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ArtifactBinding API routes — polymorphic (target_type, target_id) shape."""

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
    """Create required entities and return a dict with ids — raw SQL for ORM-deleted tables."""
    import sqlalchemy as sa
    from src.inventory.accounts.models import Account, AccountStatus
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind
    from src.platform.applications.models import Application

    sf = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
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
            resource_type='database',
            resource_key=str(uuid.uuid4()),
        )
        session.add(resource)
        await session.flush()

        account = Account(
            application_id=app.id,
            username=f'user-{uuid.uuid4().hex[:8]}',
            status=AccountStatus.active,
            meta={},
        )
        session.add(account)
        await session.flush()

        from datetime import UTC, datetime

        artifact_id = uuid.uuid4()
        await session.execute(
            sa.text(
                'INSERT INTO access_artifacts '
                '(id, application_id, artifact_type, external_id, payload, observed_at) '
                'VALUES (:id, :application_id, :artifact_type, :external_id, :payload::jsonb, :observed_at)'
            ),
            {
                'id': artifact_id,
                'application_id': app.id,
                'artifact_type': 'acl_entry',
                'external_id': str(uuid.uuid4()),
                'payload': '{"raw": "data"}',
                'observed_at': datetime(2026, 1, 1, tzinfo=UTC),
            },
        )
        await session.flush()

        from sqlalchemy import select as sa_select
        from src.inventory.actions.models import Action as RefAction

        action_id_row = await session.execute(sa_select(RefAction.id).where(RefAction.slug == 'read'))
        action_id = action_id_row.scalar_one()

        fact_id = uuid.uuid4()
        await session.execute(
            sa.text(
                'INSERT INTO access_facts '
                '(id, subject_id, resource_id, action_id, effect, observed_at) '
                'VALUES (:id, :subject_id, :resource_id, :action_id, :effect, :observed_at)'
            ),
            {
                'id': fact_id,
                'subject_id': subj.id,
                'resource_id': resource.id,
                'action_id': action_id,
                'effect': 'allow',
                'observed_at': datetime(2026, 1, 1, tzinfo=UTC),
            },
        )
        await session.commit()

        return {
            'artifact_id': artifact_id,
            'access_fact_id': fact_id,
            'resource_id': resource.id,
            'account_id': account.id,
        }


async def _seed_binding(
    engine,
    prereqs: dict,
    *,
    target_type: str,
    target_id: uuid.UUID,
) -> uuid.UUID:
    """Create an ArtifactBinding directly via repository, return binding id."""
    from src.inventory.artifact_bindings.repository import create_artifact_binding

    sf = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
    async with sf() as session:
        binding = await create_artifact_binding(
            session,
            artifact_id=prereqs['artifact_id'],
            target_type=target_type,
            target_id=target_id,
        )
        await session.commit()
        return binding.id


@pytest.mark.asyncio
async def test_list_artifact_bindings_filter_target_type(app_with_artifact_bindings, engine) -> None:
    """GET ?target_type=resource returns only resource bindings."""
    prereqs = await _make_prerequisites(engine)

    resource_binding_id = await _seed_binding(
        engine,
        prereqs,
        target_type='resource',
        target_id=prereqs['resource_id'],
    )
    await _seed_binding(
        engine,
        prereqs,
        target_type='account',
        target_id=prereqs['account_id'],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            '/api/v0/artifact-bindings',
            params={'target_type': 'resource'},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert all(r['target_type'] == 'resource' for r in data)
    assert any(r['id'] == str(resource_binding_id) for r in data)


@pytest.mark.asyncio
async def test_list_artifact_bindings_filter_target_type_and_id(app_with_artifact_bindings, engine) -> None:
    """GET ?target_type=access_fact&target_id=<uuid> returns exact provenance set."""
    prereqs = await _make_prerequisites(engine)

    fact_binding_id = await _seed_binding(
        engine,
        prereqs,
        target_type='access_fact',
        target_id=prereqs['access_fact_id'],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            '/api/v0/artifact-bindings',
            params={
                'target_type': 'access_fact',
                'target_id': str(prereqs['access_fact_id']),
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert any(r['id'] == str(fact_binding_id) for r in data)
    assert all(r['target_type'] == 'access_fact' and r['target_id'] == str(prereqs['access_fact_id']) for r in data)


@pytest.mark.asyncio
async def test_list_artifact_bindings_unknown_target_type_returns_empty(app_with_artifact_bindings, engine) -> None:
    """GET ?target_type=wat returns [] with status 200 (read-side permissiveness per Q7)."""
    prereqs = await _make_prerequisites(engine)
    await _seed_binding(
        engine,
        prereqs,
        target_type='resource',
        target_id=prereqs['resource_id'],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            '/api/v0/artifact-bindings',
            params={'target_type': 'wat'},
        )

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_artifact_binding_by_id_returns_polymorphic_shape(app_with_artifact_bindings, engine) -> None:
    """GET /{id} returns response with target_type/target_id, no old FK fields."""
    prereqs = await _make_prerequisites(engine)
    binding_id = await _seed_binding(
        engine,
        prereqs,
        target_type='resource',
        target_id=prereqs['resource_id'],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/artifact-bindings/{binding_id}')

    assert response.status_code == 200
    data = response.json()
    assert data['id'] == str(binding_id)
    assert data['artifact_id'] == str(prereqs['artifact_id'])
    assert data['target_type'] == 'resource'
    assert data['target_id'] == str(prereqs['resource_id'])
    assert 'created_at' in data
    # Old FK fields must not be present
    assert 'access_fact_id' not in data
    assert 'resource_id' not in data
    assert 'account_id' not in data


@pytest.mark.asyncio
async def test_get_artifact_binding_404(app_with_artifact_bindings) -> None:
    """GET /{id} returns 404 for unknown binding id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_artifact_bindings),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/artifact-bindings/{uuid.uuid4()}')
    assert response.status_code == 404
