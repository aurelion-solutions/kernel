# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessUsageFact API routes."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.inventory.access_usage_facts.routes import router as access_usage_facts_router


@pytest.fixture
def app_with_access_usage_facts(engine):
    """App with access usage fact routes using test engine."""
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
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(access_usage_facts_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_access_fact(engine) -> uuid.UUID:
    """Synthesize an access_fact UUID.

    Phase 15 Step 16: PG ``access_facts`` table was dropped — facts now live in
    Iceberg. ``AccessUsageFact.access_fact_id`` is a plain UUID with no FK, so
    we just return a fresh id without seeding any prerequisites.
    """
    return uuid.uuid4()


async def _make_access_fact_with_subject(engine) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (subject_id, access_fact_id).

    Phase 15 Step 16: PG ``access_facts`` table was dropped. We still need to
    create a subject for tests that filter by subject_id, but the access_fact
    id itself is just a fresh UUID.
    """
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
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
        await session.commit()
        return subj.id, uuid.uuid4()


@pytest.mark.asyncio
async def test_list_access_usage_facts_200_empty(app_with_access_usage_facts) -> None:
    """GET /access-usage-facts on empty DB returns 200 and []."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_usage_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/access-usage-facts')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_access_usage_facts_filter_by_access_fact(app_with_access_usage_facts, engine) -> None:
    """?access_fact_id= returns only matching usage facts."""
    fact_id1 = await _make_access_fact(engine)
    fact_id2 = await _make_access_fact(engine)

    w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC).isoformat()
    w_to = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC).isoformat()
    last_seen = datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_usage_facts),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/access-usage-facts',
            json={
                'access_fact_id': str(fact_id1),
                'last_seen': last_seen,
                'usage_count': 1,
                'window_from': w_from,
                'window_to': w_to,
            },
        )
        await client.post(
            '/api/v0/access-usage-facts',
            json={
                'access_fact_id': str(fact_id2),
                'last_seen': last_seen,
                'usage_count': 2,
                'window_from': w_from,
                'window_to': w_to,
            },
        )
        response = await client.get(
            '/api/v0/access-usage-facts',
            params={'access_fact_id': str(fact_id1)},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert all(r['access_fact_id'] == str(fact_id1) for r in data)


@pytest.mark.asyncio
async def test_list_access_usage_facts_filter_by_subject_joins_access_facts(
    app_with_access_usage_facts, engine
) -> None:
    """?subject_id= exercises the JOIN path against access_facts."""
    subject_id, fact_id = await _make_access_fact_with_subject(engine)

    w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC).isoformat()
    last_seen = datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_usage_facts),
        base_url='http://testserver',
    ) as client:
        r_create = await client.post(
            '/api/v0/access-usage-facts',
            json={
                'access_fact_id': str(fact_id),
                'last_seen': last_seen,
                'usage_count': 1,
                'window_from': w_from,
            },
        )
        assert r_create.status_code == 201

        response = await client.get(
            '/api/v0/access-usage-facts',
            params={'subject_id': str(subject_id)},
        )

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert data[0]['access_fact_id'] == str(fact_id)


@pytest.mark.asyncio
async def test_post_access_usage_fact_201(app_with_access_usage_facts, engine) -> None:
    """POST valid body returns 201 with id, fields round-trip correctly."""
    fact_id = await _make_access_fact(engine)
    w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC).isoformat()
    w_to = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC).isoformat()
    last_seen = datetime(2026, 1, 1, 9, 45, 0, tzinfo=UTC).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_usage_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/access-usage-facts',
            json={
                'access_fact_id': str(fact_id),
                'last_seen': last_seen,
                'usage_count': 10,
                'window_from': w_from,
                'window_to': w_to,
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['access_fact_id'] == str(fact_id)
    assert data['usage_count'] == 10
    assert data['window_to'] is not None
    assert 'created_at' in data


@pytest.mark.asyncio
async def test_post_access_usage_fact_422_inverted_window(app_with_access_usage_facts, engine) -> None:
    """POST with window_to <= window_from returns 422 with detail mentioning window_to."""
    fact_id = await _make_access_fact(engine)
    w_from = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC).isoformat()
    w_to = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC).isoformat()  # earlier

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_usage_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/access-usage-facts',
            json={
                'access_fact_id': str(fact_id),
                'last_seen': datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC).isoformat(),
                'usage_count': 1,
                'window_from': w_from,
                'window_to': w_to,
            },
        )

    assert response.status_code == 422
    assert 'window_to' in str(response.json())


@pytest.mark.asyncio
async def test_post_access_usage_fact_409_duplicate(app_with_access_usage_facts, engine) -> None:
    """Posting the same body twice returns 409 on the second call."""
    fact_id = await _make_access_fact(engine)
    payload = {
        'access_fact_id': str(fact_id),
        'last_seen': datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC).isoformat(),
        'usage_count': 1,
        'window_from': datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC).isoformat(),
        'window_to': datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC).isoformat(),
    }

    async with AsyncClient(
        transport=ASGITransport(app=app_with_access_usage_facts),
        base_url='http://testserver',
    ) as client:
        r1 = await client.post('/api/v0/access-usage-facts', json=payload)
        assert r1.status_code == 201
        r2 = await client.post('/api/v0/access-usage-facts', json=payload)
        assert r2.status_code == 409
