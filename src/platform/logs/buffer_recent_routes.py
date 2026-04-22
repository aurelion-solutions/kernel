# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Read endpoint for the most recent PostgreSQL log-buffer rows (IDE panel / tail).

Unlike ``/log-buffer``, this endpoint does not require any discriminator filter —
the intent is "give me the last N lines", equivalent to ``tail``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.logs.buffer_read_schemas import LogBufferEventRead
from src.platform.logs.buffer_repository import query_buffered_log_events

router = APIRouter(prefix='/platform/logs', tags=['platform-logs'])

_ALLOWED_LEVELS = frozenset({'debug', 'info', 'warning', 'error', 'critical'})
_q_limit = Query(50, ge=1, le=500, description='Max rows (1..500).')
_q_level = Query(None, description='Optional level filter: debug|info|warning|error|critical.')
_DependsSession = Depends(get_db)


@router.get('', response_model=list[LogBufferEventRead])
async def list_recent_logs(
    session: AsyncSession = _DependsSession,
    limit: int = _q_limit,
    level: str | None = _q_level,
) -> list[LogBufferEventRead]:
    """Return the most recent log-buffer rows, newest first.

    No discriminator required — suitable for IDE panel / ``tail`` use-cases.
    """
    if level is not None:
        norm = level.strip().lower()
        if norm not in _ALLOWED_LEVELS:
            raise HTTPException(status_code=400, detail='invalid level')
        level = norm
    rows = await query_buffered_log_events(
        session,
        level=level,
        order_desc=True,
        limit=limit,
    )
    return [LogBufferEventRead.model_validate(r) for r in rows]
