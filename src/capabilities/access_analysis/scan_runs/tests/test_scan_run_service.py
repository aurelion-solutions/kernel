# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for ScanRunService."""

from __future__ import annotations

import uuid

import pytest
from src.capabilities.access_analysis.scan_runs.exceptions import (
    ScanRunApplicationNotFoundError,
    ScanRunMissingErrorMessageError,
    ScanRunNotFoundError,
    ScanRunStatusTransitionError,
    ScanRunSubjectNotFoundError,
)
from src.capabilities.access_analysis.scan_runs.models import ScanRunStatus, ScanRunTrigger
from src.capabilities.access_analysis.scan_runs.schemas import ScanRunCreate, ScanRunStatusPatch
from src.capabilities.access_analysis.scan_runs.service import ScanRunService
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(session) -> ScanRunService:
    return ScanRunService(session, NoOpLogService())


def _make_create(
    triggered_by: ScanRunTrigger = ScanRunTrigger.manual,
    scope_subject_id: uuid.UUID | None = None,
    scope_application_id: uuid.UUID | None = None,
    created_by: str | None = None,
) -> ScanRunCreate:
    return ScanRunCreate(
        triggered_by=triggered_by,
        scope_subject_id=scope_subject_id,
        scope_application_id=scope_application_id,
        created_by=created_by,
    )


async def _insert_subject(session) -> uuid.UUID:
    """Insert a Subject (via NHI principal) and return its id."""
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    nhi = await create_nhi(
        session,
        external_id=f'test-nhi-{uuid.uuid4().hex[:8]}',
        name='Test NHI',
        kind='service_account',
    )
    subject = await create_subject(
        session,
        external_id=f'test-subject-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status=SubjectNHIStatus.active,
    )
    return subject.id


async def _insert_application(session) -> uuid.UUID:
    """Insert an Application row and return its id."""
    from src.platform.applications.models import Application

    app = Application(
        name=f'Test App {uuid.uuid4().hex[:8]}',
        code=f'APP-{uuid.uuid4().hex[:8]}',
    )
    session.add(app)
    await session.flush()
    await session.refresh(app)
    return app.id


# ---------------------------------------------------------------------------
# Create tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_manual_no_scope_succeeds(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.create(_make_create(triggered_by=ScanRunTrigger.manual))
        await session.commit()
    assert result.id > 0
    assert result.status == ScanRunStatus.pending
    assert result.started_at is None
    assert result.findings_total == 0
    assert result.findings_by_severity == {}


@pytest.mark.asyncio
async def test_create_with_valid_scope_subject_id_succeeds(session_factory) -> None:
    async with session_factory() as session:
        subject_id = await _insert_subject(session)
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.create(_make_create(scope_subject_id=subject_id))
        await session.commit()
    assert result.scope_subject_id == subject_id


@pytest.mark.asyncio
async def test_create_with_nonexistent_scope_subject_id_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(ScanRunSubjectNotFoundError):
            await svc.create(_make_create(scope_subject_id=uuid.uuid4()))


@pytest.mark.asyncio
async def test_create_with_nonexistent_scope_application_id_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(ScanRunApplicationNotFoundError):
            await svc.create(_make_create(scope_application_id=uuid.uuid4()))


# ---------------------------------------------------------------------------
# patch_status tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_status_pending_to_running_sets_started_at(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run = await svc.create(_make_create())
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.running))
        await session.commit()

    assert result.status == ScanRunStatus.running
    assert result.started_at is not None


@pytest.mark.asyncio
async def test_patch_status_running_to_completed_sets_completed_at(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run = await svc.create(_make_create())
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        running = await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.running))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.patch_status(running.id, ScanRunStatusPatch(status=ScanRunStatus.completed))
        await session.commit()

    assert result.status == ScanRunStatus.completed
    assert result.completed_at is not None
    assert result.error_message is None


@pytest.mark.asyncio
async def test_patch_status_running_to_failed_without_error_message_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run = await svc.create(_make_create())
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.running))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(ScanRunMissingErrorMessageError):
            await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.failed))


@pytest.mark.asyncio
async def test_patch_status_running_to_failed_with_error_message_succeeds(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run = await svc.create(_make_create())
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.running))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.patch_status(
            run.id,
            ScanRunStatusPatch(status=ScanRunStatus.failed, error_message='Boom'),
        )
        await session.commit()

    assert result.status == ScanRunStatus.failed
    assert result.completed_at is not None
    assert result.error_message == 'Boom'


@pytest.mark.asyncio
async def test_patch_status_pending_to_completed_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run = await svc.create(_make_create())
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(ScanRunStatusTransitionError):
            await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.completed))


@pytest.mark.asyncio
async def test_patch_status_completed_to_running_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run = await svc.create(_make_create())
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.running))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.completed))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(ScanRunStatusTransitionError):
            await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.running))


@pytest.mark.asyncio
async def test_patch_status_completed_to_completed_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run = await svc.create(_make_create())
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.running))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.completed))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(ScanRunStatusTransitionError):
            await svc.patch_status(run.id, ScanRunStatusPatch(status=ScanRunStatus.completed))


# ---------------------------------------------------------------------------
# List / filter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_with_status_filter(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        run1 = await svc.create(_make_create(triggered_by=ScanRunTrigger.manual))
        run2 = await svc.create(_make_create(triggered_by=ScanRunTrigger.api))
        await session.commit()
        await svc.patch_status(run2.id, ScanRunStatusPatch(status=ScanRunStatus.running))
        await session.commit()

        pending = await svc.list(status=ScanRunStatus.pending)
        running = await svc.list(status=ScanRunStatus.running)

    pending_ids = [r.id for r in pending]
    running_ids = [r.id for r in running]
    assert run1.id in pending_ids
    assert run2.id not in pending_ids
    assert run2.id in running_ids


@pytest.mark.asyncio
async def test_list_with_triggered_by_filter(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        r1 = await svc.create(_make_create(triggered_by=ScanRunTrigger.manual))
        r2 = await svc.create(_make_create(triggered_by=ScanRunTrigger.schedule))
        await session.commit()

        manual_runs = await svc.list(triggered_by=ScanRunTrigger.manual)
        schedule_runs = await svc.list(triggered_by=ScanRunTrigger.schedule)

    manual_ids = [r.id for r in manual_runs]
    schedule_ids = [r.id for r in schedule_runs]
    assert r1.id in manual_ids
    assert r2.id not in manual_ids
    assert r2.id in schedule_ids


@pytest.mark.asyncio
async def test_get_missing_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(ScanRunNotFoundError):
            await svc.get(999999)
