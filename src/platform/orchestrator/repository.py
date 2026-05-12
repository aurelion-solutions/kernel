# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Read-only repository helpers for the orchestrator.

Phase 18 read API — thin SELECT wrappers over platform_runs.
Engines may import these to observe pipeline-run status without
writing to orchestrator tables (Pipeline state-ownership invariant).
"""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus


async def get_pipeline_run_status(
    session: AsyncSession,
    run_id: UUID,
) -> PipelineRunStatus | None:
    """Return the current status of a pipeline run, or None if not found.

    This is the sole read path for engines that need to inspect run state
    (e.g. stale lease cleanup).  Engines MUST NOT query pipeline_runs directly
    or write to orchestrator tables.
    """
    result = await session.execute(sa.select(PipelineRun.status).where(PipelineRun.id == run_id))
    row = result.scalar_one_or_none()
    return row  # type: ignore[return-value]


_TERMINAL_STATUSES: frozenset[PipelineRunStatus] = frozenset(
    {
        PipelineRunStatus.completed,
        PipelineRunStatus.failed,
        PipelineRunStatus.failed_timeout,
        PipelineRunStatus.cancelled,
    }
)


def is_terminal(status: PipelineRunStatus) -> bool:
    """Return True when the given status is a terminal (non-recoverable) state."""
    return status in _TERMINAL_STATUSES
