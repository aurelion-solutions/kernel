# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).
"""

from __future__ import annotations

from datetime import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.scan_runs.models import ScanRun, ScanRunStatus, ScanRunTrigger


async def insert_scan_run(
    session: AsyncSession,
    *,
    triggered_by: ScanRunTrigger,
    scope_subject_id: uuid.UUID | None,
    scope_application_id: uuid.UUID | None,
    created_by: str | None,
) -> ScanRun:
    """Insert a new ScanRun row with status=pending and flush. Does not commit."""
    run = ScanRun(
        triggered_by=triggered_by,
        scope_subject_id=scope_subject_id,
        scope_application_id=scope_application_id,
        created_by=created_by,
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)
    return run


async def get_scan_run_by_id(
    session: AsyncSession,
    scan_run_id: int,
) -> ScanRun | None:
    """Return the ScanRun with the given id, or None."""
    stmt = sa.select(ScanRun).where(ScanRun.id == scan_run_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_scan_runs(
    session: AsyncSession,
    *,
    status: ScanRunStatus | None = None,
    triggered_by: ScanRunTrigger | None = None,
    scope_subject_id: uuid.UUID | None = None,
    scope_application_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ScanRun]:
    """Return ScanRuns ordered by id DESC, optionally filtered."""
    stmt = sa.select(ScanRun).order_by(ScanRun.id.desc())
    if status is not None:
        stmt = stmt.where(ScanRun.status == status)
    if triggered_by is not None:
        stmt = stmt.where(ScanRun.triggered_by == triggered_by)
    if scope_subject_id is not None:
        stmt = stmt.where(ScanRun.scope_subject_id == scope_subject_id)
    if scope_application_id is not None:
        stmt = stmt.where(ScanRun.scope_application_id == scope_application_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_scan_run_status_fields(
    session: AsyncSession,
    run: ScanRun,
    *,
    status: ScanRunStatus,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    error_message: str | None = None,
) -> ScanRun:
    """Update status + optional timestamp/error fields, flush, return refreshed entity."""
    run.status = status
    if started_at is not None:
        run.started_at = started_at
    if completed_at is not None:
        run.completed_at = completed_at
    if error_message is not None:
        run.error_message = error_message
    await session.flush()
    await session.refresh(run)
    return run


async def verify_subject_exists(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> bool:
    """Return True if a Subject with the given id exists."""
    stmt = sa.text('SELECT 1 FROM subjects WHERE id = :id LIMIT 1').bindparams(id=subject_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def verify_application_exists(
    session: AsyncSession,
    application_id: uuid.UUID,
) -> bool:
    """Return True if an Application with the given id exists."""
    stmt = sa.text('SELECT 1 FROM applications WHERE id = :id LIMIT 1').bindparams(id=application_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None
