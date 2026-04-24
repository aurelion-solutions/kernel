# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer integration tests for OrphanDetectorService — DB-backed."""

from __future__ import annotations

import uuid

import pytest
from src.capabilities.access_analysis.detectors.service import OrphanDetectorService
from src.capabilities.access_analysis.sod_rules.models import SodSeverity
from src.inventory.accounts.models import Account
from src.inventory.nhi.models import NHI
from src.inventory.ownership_assignments.models import OwnershipAssignment, OwnershipKind
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


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
    nhi = NHI(
        external_id=f'nhi-{uuid.uuid4().hex[:8]}',
        name=f'test-nhi-{uuid.uuid4().hex[:8]}',
        kind='service_account',
        owner_employee_id=None,
    )
    session.add(nhi)
    await session.flush()
    subject = Subject(
        external_id=f'subj-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def _seed_orphan_account(session, app_id: uuid.UUID, username: str = 'orphan') -> uuid.UUID:  # type: ignore[no-untyped-def]
    account = Account(
        application_id=app_id,
        username=username,
        subject_id=None,
    )
    session.add(account)
    await session.flush()
    return account.id


async def _seed_owned_account(session, app_id: uuid.UUID, subject_id: uuid.UUID, username: str = 'owned') -> uuid.UUID:  # type: ignore[no-untyped-def]
    account = Account(
        application_id=app_id,
        username=username,
        subject_id=subject_id,
    )
    session.add(account)
    await session.flush()
    return account.id


async def _seed_ownership_assignment(session, account_id: uuid.UUID, subject_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    oa = OwnershipAssignment(
        subject_id=subject_id,
        account_id=account_id,
        resource_id=None,
        kind=OwnershipKind.primary,
    )
    session.add(oa)
    await session.flush()
    return oa.id


# ---------------------------------------------------------------------------
# Test S1: 2 orphans + 1 owned → 2 findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_returns_only_orphan_findings(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subject_id = await _seed_subject(session)

        await _seed_orphan_account(session, app_id, 'orphan1')
        await _seed_orphan_account(session, app_id, 'orphan2')
        await _seed_owned_account(session, app_id, subject_id, 'owned1')
        await session.commit()

    async with session_factory() as session:
        svc = OrphanDetectorService(session)
        findings = await svc.run(application_id=None, limit=1000)

    assert len(findings) == 2
    usernames = {f.username for f in findings}
    assert usernames == {'orphan1', 'orphan2'}
    for f in findings:
        assert f.severity == SodSeverity.high


# ---------------------------------------------------------------------------
# Test S2: OwnershipAssignment surfaces last_known_owner_subject_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_surfaces_last_known_owner(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subject_id = await _seed_subject(session)

        orphan_with_owner_id = await _seed_orphan_account(session, app_id, 'with_owner')
        await _seed_orphan_account(session, app_id, 'no_owner')
        await _seed_ownership_assignment(session, orphan_with_owner_id, subject_id)
        await session.commit()

    async with session_factory() as session:
        svc = OrphanDetectorService(session)
        findings = await svc.run(application_id=None, limit=1000)

    assert len(findings) == 2
    by_username = {f.username: f for f in findings}
    assert by_username['with_owner'].last_known_owner_subject_id == subject_id
    assert by_username['no_owner'].last_known_owner_subject_id is None


# ---------------------------------------------------------------------------
# Test S3: application_id filter scopes results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_filters_by_application_id(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_a = await _seed_application(session)
        app_b = await _seed_application(session)

        await _seed_orphan_account(session, app_a, 'orphan_a')
        await _seed_orphan_account(session, app_b, 'orphan_b')
        await session.commit()

    async with session_factory() as session:
        svc = OrphanDetectorService(session)
        findings = await svc.run(application_id=app_a, limit=1000)

    assert len(findings) == 1
    assert findings[0].username == 'orphan_a'
    assert findings[0].application_id == app_a


# ---------------------------------------------------------------------------
# Test S4: limit parameter respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_respects_limit(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        app_id = await _seed_application(session)
        for i in range(5):
            await _seed_orphan_account(session, app_id, f'orphan{i:02d}')
        await session.commit()

    async with session_factory() as session:
        svc = OrphanDetectorService(session)
        findings = await svc.run(application_id=None, limit=3)

    assert len(findings) == 3


# ---------------------------------------------------------------------------
# Test S5: service does not flush, commit, emit events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_does_not_emit_or_persist(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Service never writes findings to the DB."""
    import sqlalchemy as sa
    from src.capabilities.access_analysis.findings.models import Finding

    async with session_factory() as session:
        app_id = await _seed_application(session)
        await _seed_orphan_account(session, app_id, 'ghost')
        await session.commit()

    async with session_factory() as session:
        svc = OrphanDetectorService(session)
        findings = await svc.run(application_id=None, limit=1000)
        assert len(findings) == 1

        # Confirm no Finding rows were written
        count = await session.scalar(sa.select(sa.func.count()).select_from(Finding))
        assert count == 0
