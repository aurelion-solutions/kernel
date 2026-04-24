# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer integration tests for UnusedDetectorService — DB-backed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.detectors.service import UnusedDetectorService
from src.capabilities.access_analysis.findings.models import Finding
from src.capabilities.access_analysis.sod_rules.models import SodSeverity
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


async def _get_action_id(session) -> int:  # type: ignore[no-untyped-def]
    from src.inventory.actions.models import Action as RefAction

    result = await session.execute(sa.select(RefAction.id).where(RefAction.slug == 'read'))
    return result.scalar_one()


async def _seed_access_fact(
    session,
    *,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
    valid_from: datetime,
    is_active: bool = True,
) -> uuid.UUID:  # type: ignore[no-untyped-def]
    action_id = await _get_action_id(session)
    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action_id=action_id,
        effect=AccessFactEffect.allow,
        observed_at=_NOW,
        valid_from=valid_from,
        is_active=is_active,
    )
    session.add(fact)
    await session.flush()
    return fact.id


async def _seed_usage_fact(
    session,
    *,
    access_fact_id: uuid.UUID,
    last_seen: datetime,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> None:  # type: ignore[no-untyped-def]
    if window_from is None:
        window_from = last_seen - timedelta(hours=1)
    usage = AccessUsageFact(
        access_fact_id=access_fact_id,
        last_seen=last_seen,
        usage_count=1,
        window_from=window_from,
        window_to=window_to,
    )
    session.add(usage)
    await session.flush()


# ---------------------------------------------------------------------------
# Test S1: basic scenario — 4 active facts + 1 inactive → 2 findings
#
# Each fact lives in its own resource to avoid unique-constraint conflicts.
# fact A: usage row with last_seen = now - 120 days → finding (120 >= 90)
# fact B: usage row with last_seen = now - 10 days → no finding
# fact C: no usage row, valid_from = now - 200 days → finding (200 >= 90)
# fact D: no usage row, valid_from = now - 5 days → no finding
# inactive fact: is_active=False → excluded by WHERE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_returns_expected_findings(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)

        # fact A: old usage → finding
        res_a = await _seed_resource(session, app_id)
        fact_a = await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_a,
            valid_from=_NOW - timedelta(days=150),
        )
        await _seed_usage_fact(
            session,
            access_fact_id=fact_a,
            last_seen=_NOW - timedelta(days=120),
        )

        # fact B: recent usage → no finding
        res_b = await _seed_resource(session, app_id)
        fact_b = await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_b,
            valid_from=_NOW - timedelta(days=50),
        )
        await _seed_usage_fact(
            session,
            access_fact_id=fact_b,
            last_seen=_NOW - timedelta(days=10),
        )

        # fact C: no usage, old valid_from → finding
        res_c = await _seed_resource(session, app_id)
        fact_c = await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_c,
            valid_from=_NOW - timedelta(days=200),
        )

        # fact D: no usage, new valid_from → no finding
        res_d = await _seed_resource(session, app_id)
        await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_d,
            valid_from=_NOW - timedelta(days=5),
        )

        # inactive old fact → excluded by WHERE is_active = true
        res_inactive = await _seed_resource(session, app_id)
        inactive_id = await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_inactive,
            valid_from=_NOW - timedelta(days=300),
            is_active=False,
        )
        await _seed_usage_fact(
            session,
            access_fact_id=inactive_id,
            last_seen=_NOW - timedelta(days=250),
        )

        await session.commit()

    async with session_factory() as session:
        svc = UnusedDetectorService(session)
        findings = await svc.run(application_id=None, threshold_days=90, limit=1000)

    # fact A (old usage) and fact C (no usage, old valid_from) → 2 findings
    assert len(findings) == 2
    access_fact_ids = {f.access_fact_id for f in findings}
    assert fact_a in access_fact_ids
    assert fact_c in access_fact_ids
    for f in findings:
        assert f.severity == SodSeverity.low


# ---------------------------------------------------------------------------
# Test S2: multiple usage rows for same fact — MAX last_seen wins
#
# Fact E gets two windows: (now-200d, now-5d). MAX is now-5d → no finding.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_max_last_seen_used(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_id = await _seed_resource(session, app_id)
        fact_id = await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_id,
            valid_from=_NOW - timedelta(days=300),
        )
        w1_from = _NOW - timedelta(days=210)
        w1_last = _NOW - timedelta(days=200)
        await _seed_usage_fact(
            session,
            access_fact_id=fact_id,
            last_seen=w1_last,
            window_from=w1_from,
            window_to=w1_last,
        )
        w2_from = _NOW - timedelta(days=10)
        w2_last = _NOW - timedelta(days=5)
        await _seed_usage_fact(
            session,
            access_fact_id=fact_id,
            last_seen=w2_last,
            window_from=w2_from,
            window_to=w2_last,
        )
        await session.commit()

    async with session_factory() as session:
        svc = UnusedDetectorService(session)
        findings = await svc.run(application_id=None, threshold_days=90, limit=1000)

    assert all(f.access_fact_id != fact_id for f in findings)


# ---------------------------------------------------------------------------
# Test S3: application_id filter — different app → empty list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_filters_by_application_id(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_a = await _seed_application(session)
        app_b = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_a = await _seed_resource(session, app_a)
        await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_a,
            valid_from=_NOW - timedelta(days=200),
        )
        await session.commit()

    async with session_factory() as session:
        svc = UnusedDetectorService(session)
        findings = await svc.run(application_id=app_b, threshold_days=90, limit=1000)

    assert findings == []


# ---------------------------------------------------------------------------
# Test S4: aggressive threshold (1 day) — 2-day-stale fact becomes a finding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_aggressive_threshold(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_id = await _seed_resource(session, app_id)
        fact_id = await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_id,
            valid_from=_NOW - timedelta(days=100),
        )
        await _seed_usage_fact(
            session,
            access_fact_id=fact_id,
            last_seen=_NOW - timedelta(days=2),
        )
        await session.commit()

    async with session_factory() as session:
        svc = UnusedDetectorService(session)
        findings = await svc.run(application_id=None, threshold_days=1, limit=1000)

    assert any(f.access_fact_id == fact_id for f in findings)


# ---------------------------------------------------------------------------
# Test S5: limit parameter respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_respects_limit(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)

        # create 5 facts each in its own resource (no usage → all are stale)
        for _ in range(5):
            res_id = await _seed_resource(session, app_id)
            await _seed_access_fact(
                session,
                subject_id=subj_id,
                resource_id=res_id,
                valid_from=_NOW - timedelta(days=200),
            )
        await session.commit()

    async with session_factory() as session:
        svc = UnusedDetectorService(session)
        findings = await svc.run(application_id=None, threshold_days=90, limit=3)

    assert len(findings) <= 3


# ---------------------------------------------------------------------------
# Test S6: service does not write Finding rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_does_not_persist(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_id = await _seed_resource(session, app_id)
        await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_id,
            valid_from=_NOW - timedelta(days=200),
        )
        await session.commit()

    async with session_factory() as session:
        svc = UnusedDetectorService(session)
        findings = await svc.run(application_id=None, threshold_days=90, limit=1000)
        assert len(findings) >= 1

        count = await session.scalar(sa.select(sa.func.count()).select_from(Finding))
        assert count == 0
