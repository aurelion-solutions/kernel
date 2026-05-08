# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
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
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

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
    """Orchestrates access usage fact operations and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_usage_fact(
        self,
        session: AsyncSession,
        *,
        access_fact_id: uuid.UUID,
        last_seen: datetime,
        usage_count: int = 0,
        window_from: datetime,
        window_to: datetime | None = None,
        correlation_id: str | None = None,
    ) -> AccessUsageFact:
        """Create an access usage fact.

        Validates window ordering and FK reference.
        Emits inventory.access_usage_fact.created.
        """
        if window_to is not None and window_to <= window_from:
            raise AccessUsageFactWindowOrderError('window_to must be strictly greater than window_from')

        # Phase 15: ``access_facts`` was dropped from PG — facts now live in Iceberg
        # ``normalized.access_facts``. ``access_fact_id`` is a plain UUID with no FK
        # constraint, so no existence check is performed here.

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
            if pgcode == '23505':
                raise AccessUsageFactDuplicateError(
                    'Usage fact already exists for this (access_fact_id, window_from, window_to)'
                ) from exc
            if pgcode == '23514':
                raise AccessUsageFactWindowOrderError(
                    'CHECK constraint violated (usage_count or window ordering)'
                ) from exc
            raise

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.access_usage_fact.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'usage_fact_id': str(usage_fact.id),
                    'access_fact_id': str(access_fact_id),
                    'last_seen': last_seen.isoformat(),
                    'usage_count': usage_count,
                    'window_from': window_from.isoformat(),
                    'window_to': window_to.isoformat() if window_to is not None else None,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(usage_fact.id),
            )
        )
        return usage_fact

    async def get_usage_fact(
        self,
        session: AsyncSession,
        usage_fact_id: uuid.UUID,
    ) -> AccessUsageFact | None:
        """Get access usage fact by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_by_id(session, usage_fact_id)

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
        """List access usage facts with optional filters. No event emitted."""
        return await repo_list(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            access_fact_id=access_fact_id,
            since=since,
            limit=limit,
            offset=offset,
        )
