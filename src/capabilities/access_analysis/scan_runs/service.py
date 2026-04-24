# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun service — business logic for the ScanRun slice.

No events and no logs are emitted by this service — scan.* events are
emitted by the engine (Step 14), not by CRUD-only status PATCHes.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.scan_runs.exceptions import (
    ScanRunApplicationNotFoundError,
    ScanRunMissingErrorMessageError,
    ScanRunNotFoundError,
    ScanRunStatusTransitionError,
    ScanRunSubjectNotFoundError,
)
from src.capabilities.access_analysis.scan_runs.models import ScanRunStatus, ScanRunTrigger
from src.capabilities.access_analysis.scan_runs.repository import (
    get_scan_run_by_id,
    insert_scan_run,
    list_scan_runs,
    update_scan_run_status_fields,
    verify_application_exists,
    verify_subject_exists,
)
from src.capabilities.access_analysis.scan_runs.schemas import (
    ScanRunCreate,
    ScanRunRead,
    ScanRunStatusPatch,
)
from src.platform.logs.service import LogService

# Allowed status transitions: (from, to)
_ALLOWED_TRANSITIONS: frozenset[tuple[ScanRunStatus, ScanRunStatus]] = frozenset(
    [
        (ScanRunStatus.pending, ScanRunStatus.running),
        (ScanRunStatus.running, ScanRunStatus.completed),
        (ScanRunStatus.running, ScanRunStatus.failed),
    ]
)


def _validate_status_transition(
    from_status: ScanRunStatus,
    to_status: ScanRunStatus,
) -> None:
    """Raise ScanRunStatusTransitionError if the transition is not allowed."""
    if (from_status, to_status) not in _ALLOWED_TRANSITIONS:
        raise ScanRunStatusTransitionError(from_status, to_status)


class ScanRunService:
    """CRUD service for the ScanRun slice.

    ``log_service`` is plumbed for parity with other slices but is not used in
    this step — event emission is the engine's responsibility (Step 14).
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def create(self, payload: ScanRunCreate) -> ScanRunRead:
        """Create a new ScanRun in status=pending.

        Verifies scope subject/application existence when provided.
        ``created_by`` is supplied from the request body for now —
        will be replaced with auth context in a future step.
        """
        if payload.scope_subject_id is not None:
            exists = await verify_subject_exists(self._session, payload.scope_subject_id)
            if not exists:
                raise ScanRunSubjectNotFoundError(payload.scope_subject_id)

        if payload.scope_application_id is not None:
            exists = await verify_application_exists(self._session, payload.scope_application_id)
            if not exists:
                raise ScanRunApplicationNotFoundError(payload.scope_application_id)

        run = await insert_scan_run(
            self._session,
            triggered_by=payload.triggered_by,
            scope_subject_id=payload.scope_subject_id,
            scope_application_id=payload.scope_application_id,
            created_by=payload.created_by,
        )
        return ScanRunRead.model_validate(run)

    async def list(
        self,
        *,
        status: ScanRunStatus | None = None,
        triggered_by: ScanRunTrigger | None = None,
        scope_subject_id: uuid.UUID | None = None,
        scope_application_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ScanRunRead]:
        """Return ScanRuns, optionally filtered."""
        rows = await list_scan_runs(
            self._session,
            status=status,
            triggered_by=triggered_by,
            scope_subject_id=scope_subject_id,
            scope_application_id=scope_application_id,
            limit=limit,
            offset=offset,
        )
        return [ScanRunRead.model_validate(row) for row in rows]

    async def get(self, scan_run_id: int) -> ScanRunRead:
        """Return a ScanRun by id. Raises ScanRunNotFoundError when missing."""
        run = await get_scan_run_by_id(self._session, scan_run_id)
        if run is None:
            raise ScanRunNotFoundError(scan_run_id)
        return ScanRunRead.model_validate(run)

    async def patch_status(self, scan_run_id: int, payload: ScanRunStatusPatch) -> ScanRunRead:
        """Transition a ScanRun's status.

        Allowed transitions:
          pending  → running   (sets started_at)
          running  → completed (sets completed_at)
          running  → failed    (sets completed_at; requires error_message)

        Raises ScanRunStatusTransitionError for any other transition.
        Raises ScanRunMissingErrorMessageError when transitioning to 'failed'
        without an error_message.
        """
        run = await get_scan_run_by_id(self._session, scan_run_id)
        if run is None:
            raise ScanRunNotFoundError(scan_run_id)

        _validate_status_transition(run.status, payload.status)

        if payload.status == ScanRunStatus.failed and not payload.error_message:
            raise ScanRunMissingErrorMessageError()

        now = datetime.now(tz=UTC)
        started_at: datetime | None = None
        completed_at: datetime | None = None

        if payload.status == ScanRunStatus.running:
            started_at = now
        elif payload.status in (ScanRunStatus.completed, ScanRunStatus.failed):
            completed_at = now

        run = await update_scan_run_status_fields(
            self._session,
            run,
            status=payload.status,
            started_at=started_at,
            completed_at=completed_at,
            error_message=payload.error_message,
        )
        return ScanRunRead.model_validate(run)
