# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Read API for the short-term PostgreSQL ``log_event_buffer`` (debug buffer)."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.logs.buffer_read_schemas import LogBufferEventRead
from src.platform.logs.buffer_repository import query_buffered_log_events

router = APIRouter(prefix='/log-buffer', tags=['log-buffer'])

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000

DependsSession = Depends(get_db)

SortOrder = Literal['asc', 'desc']

_q_correlation_id = Query(None, description='Filter by correlation id.')
_q_target_type = Query(None, description='Requires target_id when set.')
_q_target_id = Query(None, description='Requires target_type when set.')
_q_initiator_type = Query(None, description='Requires initiator_id when set.')
_q_initiator_id = Query(None, description='Requires initiator_type when set.')
_q_actor_type = Query(None, description='Requires actor_id when set.')
_q_actor_id = Query(None, description='Requires actor_type when set.')
_q_level = Query(None, description='Filter by level (e.g. info, error).')
_q_payload_step_run_id = Query(
    None,
    description="Filter by payload->>'step_run_id' (set by the runner and the step-scoped log façade).",
)
_q_from_ts = Query(None, description='Inclusive lower bound on event timestamp (ISO 8601).')
_q_to_ts = Query(None, description='Inclusive upper bound on event timestamp (ISO 8601).')
_q_order = Query(
    'desc',
    description='Sort by event timestamp: asc (chronological) or desc (newest first).',
)
_q_limit = Query(
    _DEFAULT_LIMIT,
    ge=1,
    le=_MAX_LIMIT,
    description=f'Max rows to return (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT}).',
)


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _lower_opt(value: str | None) -> str | None:
    v = _blank_to_none(value)
    return v.lower() if v is not None else None


@router.get('', response_model=list[LogBufferEventRead])
async def list_buffered_logs(
    session: AsyncSession = DependsSession,
    correlation_id: str | None = _q_correlation_id,
    target_type: str | None = _q_target_type,
    target_id: str | None = _q_target_id,
    initiator_type: str | None = _q_initiator_type,
    initiator_id: str | None = _q_initiator_id,
    actor_type: str | None = _q_actor_type,
    actor_id: str | None = _q_actor_id,
    level: str | None = _q_level,
    payload_step_run_id: str | None = _q_payload_step_run_id,
    from_ts: datetime | None = _q_from_ts,
    to_ts: datetime | None = _q_to_ts,
    order: SortOrder = _q_order,
    limit: int = _q_limit,
) -> list[LogBufferEventRead]:
    """Query buffered normalized log events from PostgreSQL only (not SIEM)."""
    correlation_id = _blank_to_none(correlation_id)
    target_id = _blank_to_none(target_id)
    initiator_id = _blank_to_none(initiator_id)
    actor_id = _blank_to_none(actor_id)
    payload_step_run_id = _blank_to_none(payload_step_run_id)

    target_type = _lower_opt(target_type)
    initiator_type = _lower_opt(initiator_type)
    actor_type = _lower_opt(actor_type)
    level = _lower_opt(level)

    if (target_type is None) != (target_id is None):
        raise HTTPException(
            status_code=400,
            detail='target_type and target_id must be provided together',
        )
    if (initiator_type is None) != (initiator_id is None):
        raise HTTPException(
            status_code=400,
            detail='initiator_type and initiator_id must be provided together',
        )
    if (actor_type is None) != (actor_id is None):
        raise HTTPException(
            status_code=400,
            detail='actor_type and actor_id must be provided together',
        )

    has_discriminator = (
        correlation_id is not None
        or (target_type is not None and target_id is not None)
        or (initiator_type is not None and initiator_id is not None)
        or (actor_type is not None and actor_id is not None)
        or level is not None
        or payload_step_run_id is not None
    )
    if not has_discriminator:
        raise HTTPException(
            status_code=400,
            detail=(
                'Provide at least one filter: correlation_id, target_type+target_id, '
                'initiator_type+initiator_id, actor_type+actor_id, level, or payload_step_run_id'
            ),
        )

    rows = await query_buffered_log_events(
        session,
        correlation_id=correlation_id,
        target_type=target_type,
        target_id=target_id,
        initiator_type=initiator_type,
        initiator_id=initiator_id,
        actor_type=actor_type,
        actor_id=actor_id,
        level=level,
        payload_step_run_id=payload_step_run_id,
        from_ts=from_ts,
        to_ts=to_ts,
        order_desc=(order == 'desc'),
        limit=limit,
    )
    return [LogBufferEventRead.model_validate(r) for r in rows]
