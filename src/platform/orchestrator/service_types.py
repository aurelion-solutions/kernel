# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DTOs and exceptions for the pipeline orchestrator service.

Kept separate from service.py so that downstream callers (routes, runner,
matcher) can import the exception types without pulling in the full service
stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import uuid

from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus


@dataclass(frozen=True, slots=True)
class PipelineRunCreateResult:
    """Result of :meth:`PipelineOrchestratorService.create_pipeline_run`.

    ``created=False`` means an in-flight duplicate was found and returned
    instead of inserting a new row.  Callers must not treat this as an error.
    """

    run: PipelineRun
    created: bool


@dataclass(frozen=True, slots=True)
class ReclaimResult:
    """Result of :meth:`PipelineOrchestratorService.reclaim_step`."""

    aborted_step_run_id: uuid.UUID
    new_step_run_id: uuid.UUID
    new_attempt: int


class OrchestratorStateConflict(Exception):
    """Raised when a status-guarded UPDATE finds the row in an unexpected state.

    ``actual=None`` when the row does not exist at conflict-check time.
    """

    def __init__(
        self,
        *,
        run_id: uuid.UUID,
        expected: tuple[PipelineRunStatus, ...],
        actual: PipelineRunStatus | None,
    ) -> None:
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f'Pipeline run {run_id} expected status in '
            f'{[s.value for s in expected]} but found '
            f'{actual.value if actual else "missing"}'
        )


class OrchestratorRowMissing(Exception):
    """Raised when the target row is absent from the database."""


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """Result of :meth:`PipelineOrchestratorService.request_cancel`.

    ``sync=True`` means the run was cancelled synchronously (pending/awaiting_event
    branch); the caller can expect status='cancelled' immediately.
    ``sync=False`` means cancelling was requested for an in-flight run; the runner
    watcher owns the terminal transition.
    """

    run_id: uuid.UUID
    status: PipelineRunStatus
    sync: bool


class AlreadyCancellingError(Exception):
    """Raised when request_cancel is called on a run already in 'cancelling' status."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        super().__init__(f'Pipeline run {run_id} is already cancelling')


class TerminalStatusError(Exception):
    """Raised when request_cancel is called on a run already in a terminal status."""

    def __init__(self, run_id: uuid.UUID, status: PipelineRunStatus) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f'Pipeline run {run_id} is in terminal status: {status.value}')


class RunNotRetryableError(Exception):
    """Raised when create_retry is called on a run that cannot be retried.

    reason='cancelling'   -- source run is in 'cancelling' status
    reason='non_terminal' -- source run is pending/running/awaiting_event
    """

    def __init__(
        self,
        run_id: uuid.UUID,
        status: PipelineRunStatus,
        reason: Literal['cancelling', 'non_terminal'],
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.reason = reason
        super().__init__(f'Pipeline run {run_id} is not retryable (status={status.value}, reason={reason})')
