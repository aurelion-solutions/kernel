# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact service — business logic and operational log emission."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_usage_facts.models import AccessUsageFact
from src.inventory.access_usage_facts.repository import (
    create_access_usage_fact as repo_create,
)
from src.inventory.access_usage_facts.repository import (
    get_access_usage_fact_by_id as repo_get_by_id,
)
from src.inventory.access_usage_facts.repository import (
    list_access_usage_facts as repo_list,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

_COMPONENT = 'inventory.access_usage_facts'


class AccessUsageFactNotFoundError(Exception):
    """Raised when usage fact is not found."""

    def __init__(self, usage_fact_id: uuid.UUID) -> None:
        self.usage_fact_id = usage_fact_id
        super().__init__(f'Access usage fact not found: {usage_fact_id}')


class AccessUsageFactForeignKeyError(Exception):
    """Raised when referenced AccessFact is not found."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AccessUsageFactWindowOrderError(Exception):
    """Raised when window_to <= window_from."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AccessUsageFactDuplicateError(Exception):
    """Raised when unique (access_fact_id, window_from, window_to) is violated."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AccessUsageFactService:
    """Orchestrates access usage fact operations and operational log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_usage_fact(
        self,
        session: AsyncSession,
        *,
        access_fact_id: uuid.UUID,
        last_seen: datetime,
        usage_count: int = 0,
        window_from: datetime,
        window_to: datetime | None = None,
    ) -> AccessUsageFact:
        """Create an access usage fact. Validates window ordering and FK reference."""
        if window_to is not None and window_to <= window_from:
            raise AccessUsageFactWindowOrderError('window_to must be strictly greater than window_from')

        from src.inventory.access_facts.models import AccessFact

        fact = await session.get(AccessFact, access_fact_id)
        if fact is None:
            raise AccessUsageFactForeignKeyError(f'AccessFact not found: {access_fact_id}')

        try:
            usage_fact = await repo_create(
                session,
                access_fact_id=access_fact_id,
                last_seen=last_seen,
                usage_count=usage_count,
                window_from=window_from,
                window_to=window_to,
            )
        except IntegrityError as exc:
            await session.rollback()
            pgcode = getattr(exc.orig, 'pgcode', None) or getattr(exc.orig, 'sqlstate', None)
            if pgcode == '23503':
                raise AccessUsageFactForeignKeyError('Referenced AccessFact not found (concurrent delete)') from exc
            if pgcode == '23505':
                raise AccessUsageFactDuplicateError(
                    'Usage fact already exists for this (access_fact_id, window_from, window_to)'
                ) from exc
            if pgcode == '23514':
                raise AccessUsageFactWindowOrderError(
                    'CHECK constraint violated (usage_count or window ordering)'
                ) from exc
            raise

        self._log.emit_safe(
            'access_usage_fact.created',
            LogLevel.INFO,
            'Access usage fact created',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'usage_fact_id': str(usage_fact.id),
                    'access_fact_id': str(access_fact_id),
                    'last_seen': last_seen.isoformat(),
                    'usage_count': usage_count,
                    'window_from': window_from.isoformat(),
                    'window_to': window_to.isoformat() if window_to is not None else None,
                },
                actor_component=_COMPONENT,
                target_id='access_usage_fact',
            ),
        )
        return usage_fact

    async def get_usage_fact(
        self,
        session: AsyncSession,
        usage_fact_id: uuid.UUID,
    ) -> AccessUsageFact | None:
        """Get access usage fact by id. Logs retrieval when found."""
        usage_fact = await repo_get_by_id(session, usage_fact_id)
        if usage_fact is not None:
            self._log.emit_safe(
                'access_usage_fact.retrieved',
                LogLevel.INFO,
                'Access usage fact retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {'usage_fact_id': str(usage_fact_id)},
                    actor_component=_COMPONENT,
                    target_id='access_usage_fact',
                ),
            )
        return usage_fact

    async def list_usage_facts(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        access_fact_id: uuid.UUID | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AccessUsageFact]:
        """List access usage facts with optional filters. No logging."""
        return await repo_list(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            access_fact_id=access_fact_id,
            since=since,
            limit=limit,
            offset=offset,
        )
