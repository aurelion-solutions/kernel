# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for POST /subjects/bulk route."""

from collections.abc import AsyncGenerator
from typing import Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.inventory.employees.repository import create_employee
from src.inventory.persons.repository import create_person
from src.inventory.subjects.models import SubjectEmployeeStatus, SubjectKind
from src.inventory.subjects.repository import create_subject
from src.inventory.subjects.routes import router as subjects_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_subjects(engine: AsyncEngine) -> FastAPI:
    """FastAPI app with subjects router and test DB session."""
    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(subjects_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture
async def two_persons_with_employees(engine: AsyncEngine) -> list[str]:
    """Pre-seed 2 persons+employees, return person external_ids."""
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        p1 = await create_person(session, external_id='bulk-rt-subj-p1', full_name='R1')
        p2 = await create_person(session, external_id='bulk-rt-subj-p2', full_name='R2')
        await session.flush()
        await create_employee(session, person_id=p1.id)
        await create_employee(session, person_id=p2.id)
        await session.commit()
        return [p1.external_id, p2.external_id]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_bulk_returns_200_with_ids(
    app_with_subjects: FastAPI,
    two_persons_with_employees: list[str],
) -> None:
    """Happy path: 2 items → 200 with upserted=2 and 2 valid UUIDs."""
    payload = {
        'items': [
            {'external_id': f'bulk-rt-ext-{i}', 'person_external_id': eid}
            for i, eid in enumerate(two_persons_with_employees)
        ]
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data['upserted'] == 2
    assert len(data['ids']) == 2
    for subject_id in data['ids']:
        uuid.UUID(subject_id)  # validates format


@pytest.mark.asyncio
async def test_post_bulk_unknown_person_returns_422(
    app_with_subjects: FastAPI,
) -> None:
    """Non-existent person_external_id → 422 with detail mentioning the id."""
    ghost_id = 'ghost-person-bulk-route-xyz'
    payload = {'items': [{'external_id': 'some-ext', 'person_external_id': ghost_id}]}
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json=payload)

    assert response.status_code == 422
    assert ghost_id in response.json()['detail']


@pytest.mark.asyncio
async def test_post_bulk_person_without_employee_returns_422(
    app_with_subjects: FastAPI,
    engine: AsyncEngine,
) -> None:
    """Person exists but no employee row → 422 with person_external_id in detail."""
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        await create_person(session, external_id='bulk-rt-nomp-p', full_name='NoEmp')
        await session.commit()

    payload = {'items': [{'external_id': 'some-ext-3', 'person_external_id': 'bulk-rt-nomp-p'}]}
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json=payload)

    assert response.status_code == 422
    assert 'bulk-rt-nomp-p' in response.json()['detail']


@pytest.mark.asyncio
async def test_post_bulk_employee_already_bound_returns_409(
    app_with_subjects: FastAPI,
    engine: AsyncEngine,
) -> None:
    """Employee already bound to different Subject → 409."""
    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        p = await create_person(session, external_id='bulk-rt-bound-p', full_name='B')
        await session.flush()
        emp = await create_employee(session, person_id=p.id)
        await session.flush()
        await create_subject(
            session,
            external_id='bound-subj-A',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status=SubjectEmployeeStatus.active,
        )
        await session.commit()

    # Try to bind same employee to a DIFFERENT external_id
    payload = {'items': [{'external_id': 'bound-subj-B', 'person_external_id': 'bulk-rt-bound-p'}]}
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json=payload)

    assert response.status_code == 409
    detail = response.json().get('detail', '')
    assert 'already bound' in detail.lower() or '->' in detail


@pytest.mark.asyncio
async def test_post_bulk_duplicate_business_key_returns_422(
    app_with_subjects: FastAPI,
    two_persons_with_employees: list[str],
) -> None:
    """Two items with the same (kind, external_id) → 422 from model_validator."""
    eid = two_persons_with_employees[0]
    payload = {
        'items': [
            {'external_id': 'dup-ext-key', 'person_external_id': eid},
            {'external_id': 'dup-ext-key', 'person_external_id': eid},
        ]
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_bulk_invalid_kind_returns_422(
    app_with_subjects: FastAPI,
) -> None:
    """kind='nhi' → 422 from Pydantic Literal validation."""
    payload = {
        'items': [
            {
                'external_id': 'some-ext',
                'person_external_id': 'any-person',
                'kind': 'nhi',
            }
        ]
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_bulk_invalid_status_for_employee_returns_422(
    app_with_subjects: FastAPI,
) -> None:
    """status='registered' (customer-only) → 422 from SubjectEmployeeStatus enum validation."""
    payload = {
        'items': [
            {
                'external_id': 'some-ext',
                'person_external_id': 'any-person',
                'status': 'registered',
            }
        ]
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_bulk_empty_items_returns_422(
    app_with_subjects: FastAPI,
) -> None:
    """Empty items list → 422 (min_length=1)."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json={'items': []})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_bulk_too_many_items_returns_422(
    app_with_subjects: FastAPI,
) -> None:
    """501 items → 422 (max_length=500)."""
    items: list[Any] = [{'external_id': f'bulk-over-{i}', 'person_external_id': f'p-{i}'} for i in range(501)]
    async with AsyncClient(
        transport=ASGITransport(app=app_with_subjects),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/subjects/bulk', json={'items': items})

    assert response.status_code == 422
