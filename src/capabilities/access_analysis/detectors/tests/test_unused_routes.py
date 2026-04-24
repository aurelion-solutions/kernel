# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for POST /access-analysis/detect-unused."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.findings.models import Finding
from src.inventory.access_facts.models import AccessFact, AccessFactEffect
from src.inventory.access_usage_facts.models import AccessUsageFact
from src.inventory.employees.repository import create_employee
from src.inventory.persons.repository import create_person
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)


async def _seed_application(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    app = Application(
        name=f'app-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _seed_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
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
    return subj.id


async def _seed_resource(session, app_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app_id,
        kind='database',
        resource_type='database',
        resource_key=str(uuid.uuid4()),
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _get_action_id(session, slug: str = 'read') -> int:  # type: ignore[no-untyped-def]
    from src.inventory.actions.models import Action as RefAction

    result = await session.execute(sa.select(RefAction.id).where(RefAction.slug == slug))
    return result.scalar_one()


async def _seed_stale_fact(session, app_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Create an active AccessFact with a usage row last_seen 100 days ago."""
    subj_id = await _seed_subject(session)
    res_id = await _seed_resource(session, app_id)
    action_id = await _get_action_id(session)
    fact = AccessFact(
        subject_id=subj_id,
        resource_id=res_id,
        action_id=action_id,
        effect=AccessFactEffect.allow,
        observed_at=_NOW,
        valid_from=_NOW - timedelta(days=150),
        is_active=True,
    )
    session.add(fact)
    await session.flush()
    usage = AccessUsageFact(
        access_fact_id=fact.id,
        last_seen=_NOW - timedelta(days=100),
        usage_count=1,
        window_from=_NOW - timedelta(days=101),
        window_to=_NOW - timedelta(days=100),
    )
    session.add(usage)
    await session.flush()
    return fact.id


# ---------------------------------------------------------------------------
# A1: empty body, no facts seeded → 200, []
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_empty_body_no_results(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-unused', json={})
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# A2: one stale-usage fact → 200, one UnusedFindingResponse with all fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_returns_finding(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        fact_id = await _seed_stale_fact(session, app_id)
        await session.commit()

    resp = await client.post('/api/v0/access-analysis/detect-unused', json={})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1

    finding = data[0]
    assert finding['access_fact_id'] == str(fact_id)
    assert finding['severity'] == 'low'
    assert finding['unused_for_days'] >= 90
    assert isinstance(finding['subject_id'], str)
    assert isinstance(finding['resource_id'], str)
    assert isinstance(finding['application_id'], str)
    assert finding['last_seen'] is not None  # usage exists


# ---------------------------------------------------------------------------
# A3: application_id filter → only matching facts returned
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_application_id_filter(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_a = await _seed_application(session)
        app_b = await _seed_application(session)
        fact_a = await _seed_stale_fact(session, app_a)
        await _seed_stale_fact(session, app_b)
        await session.commit()

    resp = await client.post(
        '/api/v0/access-analysis/detect-unused',
        json={'application_id': str(app_a)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]['access_fact_id'] == str(fact_a)
    assert data[0]['application_id'] == str(app_a)


# ---------------------------------------------------------------------------
# A4: threshold_days=0 → 422 (ge=1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_threshold_zero_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-unused', json={'threshold_days': 0})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# A5: threshold_days=10000 → 422 (le=3650)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_threshold_exceeds_cap_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-unused', json={'threshold_days': 10000})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# A6: limit=0 → 422 (ge=1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_limit_zero_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-unused', json={'limit': 0})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# A7: limit=10000 → 422 (le=5000)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_limit_exceeds_cap_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post('/api/v0/access-analysis/detect-unused', json={'limit': 10000})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# A8: extra field in body → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_extra_field_422(client) -> None:  # type: ignore[no-untyped-def]
    resp = await client.post(
        '/api/v0/access-analysis/detect-unused',
        json={'extra_field': 'bad'},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# A9: default body {} → uses 90-day threshold and 1000 limit defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_default_body_uses_defaults(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """With a fact having last_seen = 89 days ago, default 90-day threshold → no finding."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_id = await _seed_resource(session, app_id)
        action_id = await _get_action_id(session)
        fact = AccessFact(
            subject_id=subj_id,
            resource_id=res_id,
            action_id=action_id,
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
            valid_from=_NOW - timedelta(days=100),
            is_active=True,
        )
        session.add(fact)
        await session.flush()
        usage = AccessUsageFact(
            access_fact_id=fact.id,
            last_seen=_NOW - timedelta(days=89),
            usage_count=1,
            window_from=_NOW - timedelta(days=90),
            window_to=_NOW - timedelta(days=89),
        )
        session.add(usage)
        await session.flush()
        await session.commit()

    resp = await client.post('/api/v0/access-analysis/detect-unused', json={})
    assert resp.status_code == 200
    # 89 days < 90 threshold → no finding
    assert resp.json() == []


# ---------------------------------------------------------------------------
# A10: no Finding rows written after detection call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_unused_writes_no_findings(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        await _seed_stale_fact(session, app_id)
        await session.commit()

    resp = await client.post('/api/v0/access-analysis/detect-unused', json={})
    assert resp.status_code == 200
    assert len(resp.json()) >= 1

    async with session_factory() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Finding))
    assert count == 0
