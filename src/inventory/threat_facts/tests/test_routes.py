# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ThreatFact API routes."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.inventory.threat_facts.routes import router as threat_facts_router


@pytest.fixture
def app_with_threat_facts(engine):
    """App with threat fact routes using test engine."""
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
    app.include_router(threat_facts_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_subject(engine) -> uuid.UUID:
    """Create minimal subject; return subject.id."""
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


@pytest.mark.asyncio
async def test_list_threat_facts_200_empty(app_with_threat_facts) -> None:
    """GET /api/v0/threat-facts on empty DB returns 200 and []."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_threat_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/threat-facts')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_threat_facts_filter_by_subject(app_with_threat_facts, engine) -> None:
    """?subject_id= returns only facts for that subject."""
    subject_id1 = await _make_subject(engine)
    subject_id2 = await _make_subject(engine)

    observed = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_threat_facts),
        base_url='http://testserver',
    ) as client:
        r1 = await client.put(
            f'/api/v0/threat-facts/{subject_id1}',
            json={'risk_score': 0.3, 'observed_at': observed},
        )
        assert r1.status_code == 201
        r2 = await client.put(
            f'/api/v0/threat-facts/{subject_id2}',
            json={'risk_score': 0.7, 'observed_at': observed},
        )
        assert r2.status_code == 201

        response = await client.get('/api/v0/threat-facts', params={'subject_id': str(subject_id1)})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]['subject_id'] == str(subject_id1)


@pytest.mark.asyncio
async def test_list_threat_facts_min_risk_score_filter(app_with_threat_facts, engine) -> None:
    """?min_risk_score=0.5 returns only rows with risk_score >= 0.5."""
    subject_id1 = await _make_subject(engine)
    subject_id2 = await _make_subject(engine)
    subject_id3 = await _make_subject(engine)

    observed = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_threat_facts),
        base_url='http://testserver',
    ) as client:
        await client.put(f'/api/v0/threat-facts/{subject_id1}', json={'risk_score': 0.2, 'observed_at': observed})
        await client.put(f'/api/v0/threat-facts/{subject_id2}', json={'risk_score': 0.6, 'observed_at': observed})
        await client.put(f'/api/v0/threat-facts/{subject_id3}', json={'risk_score': 0.9, 'observed_at': observed})

        response = await client.get('/api/v0/threat-facts', params={'min_risk_score': '0.5'})

    assert response.status_code == 200
    data = response.json()
    scores = [r['risk_score'] for r in data]
    assert all(s >= 0.5 for s in scores)
    assert len(scores) == 2


@pytest.mark.asyncio
async def test_put_threat_fact_201_on_first_insert(app_with_threat_facts, engine) -> None:
    """PUT valid body returns 201; response body matches payload; DB row exists."""
    subject_id = await _make_subject(engine)
    observed = datetime(2026, 1, 15, tzinfo=UTC).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_threat_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.put(
            f'/api/v0/threat-facts/{subject_id}',
            json={
                'risk_score': 0.75,
                'active_indicators': ['credential_stuffing'],
                'failed_auth_count': 5,
                'observed_at': observed,
            },
        )

    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['subject_id'] == str(subject_id)
    assert data['risk_score'] == 0.75
    assert data['active_indicators'] == ['credential_stuffing']
    assert data['failed_auth_count'] == 5


@pytest.mark.asyncio
async def test_put_threat_fact_200_on_subsequent_update(app_with_threat_facts, engine) -> None:
    """Second PUT returns 200; updated_at >= created_at; id unchanged."""
    subject_id = await _make_subject(engine)
    observed = datetime(2026, 1, 15, tzinfo=UTC).isoformat()

    async with AsyncClient(
        transport=ASGITransport(app=app_with_threat_facts),
        base_url='http://testserver',
    ) as client:
        r1 = await client.put(
            f'/api/v0/threat-facts/{subject_id}',
            json={'risk_score': 0.3, 'observed_at': observed},
        )
        assert r1.status_code == 201
        fact_id = r1.json()['id']

        r2 = await client.put(
            f'/api/v0/threat-facts/{subject_id}',
            json={'risk_score': 0.8, 'observed_at': observed},
        )
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2['id'] == fact_id
        assert data2['risk_score'] == 0.8


@pytest.mark.asyncio
async def test_put_threat_fact_422_invalid_risk_score(app_with_threat_facts, engine) -> None:
    """PUT with risk_score=1.5 returns 422; detail mentions risk_score."""
    subject_id = await _make_subject(engine)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_threat_facts),
        base_url='http://testserver',
    ) as client:
        response = await client.put(
            f'/api/v0/threat-facts/{subject_id}',
            json={'risk_score': 1.5},
        )

    assert response.status_code == 422
    assert 'risk_score' in str(response.json())
