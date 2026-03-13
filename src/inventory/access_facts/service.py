# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_facts.models import AccessFact, AccessFactEffect
from src.inventory.access_facts.repository import (
    create_access_fact as repo_create_access_fact,
)
from src.inventory.access_facts.repository import (
    get_access_fact_by_id as repo_get_access_fact_by_id,
)
from src.inventory.access_facts.repository import (
    get_access_fact_by_natural_key as repo_get_access_fact_by_natural_key,
)
from src.inventory.access_facts.repository import (
    invalidate_access_fact as repo_invalidate_access_fact,
)
from src.inventory.access_facts.repository import (
    list_access_facts as repo_list_access_facts,
)
from src.inventory.enums import Action
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

_COMPONENT = 'inventory.access_facts'


class AccessFactNotFoundError(Exception):
    """Raised when an access fact is not found."""

    def __init__(self, fact_id: uuid.UUID) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact not found: {fact_id}')


class DuplicateAccessFactError(Exception):
    """Raised when a duplicate access fact is detected (unique constraint violation)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AccessFactForeignKeyError(Exception):
    """Raised when a referenced entity (subject, resource, account) does not exist."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AccessFactService:
    """Orchestrates access fact creation, retrieval, invalidation, and log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_fact(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID,
        account_id: uuid.UUID | None = None,
        resource_id: uuid.UUID,
        action: Action,
        effect: AccessFactEffect,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> AccessFact:
        """Create an access fact. Validates FK targets exist. Emits access_fact.created."""
        from src.inventory.subjects.models import Subject

        if await session.get(Subject, subject_id) is None:
            raise AccessFactForeignKeyError(f'Subject not found: {subject_id}')

        from src.inventory.resources.models import Resource

        if await session.get(Resource, resource_id) is None:
            raise AccessFactForeignKeyError(f'Resource not found: {resource_id}')

        if account_id is not None:
            from src.inventory.accounts.models import Account

            if await session.get(Account, account_id) is None:
                raise AccessFactForeignKeyError(f'Account not found: {account_id}')

        try:
            fact = await repo_create_access_fact(
                session,
                subject_id=subject_id,
                account_id=account_id,
                resource_id=resource_id,
                action=action,
                effect=effect,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        except IntegrityError as exc:
            orig = exc.orig
            pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
            if pgcode == '23505':
                raise DuplicateAccessFactError(
                    f'Duplicate access fact: subject={subject_id} resource={resource_id}'
                    f' action={action} effect={effect}'
                ) from exc
            raise AccessFactForeignKeyError(str(exc)) from exc

        self._log.emit_safe(
            'access_fact.created',
            LogLevel.INFO,
            'Access fact created',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'access_fact_id': str(fact.id),
                    'subject_id': str(subject_id),
                    'account_id': str(account_id) if account_id else None,
                    'resource_id': str(resource_id),
                    'action': action.value,
                    'effect': effect.value,
                    'valid_from': str(fact.valid_from),
                    'valid_until': str(fact.valid_until) if fact.valid_until else None,
                },
                actor_component=_COMPONENT,
                target_id='access_fact',
            ),
        )
        return fact

    async def get_fact_by_natural_key(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID,
        account_id: uuid.UUID | None,
        resource_id: uuid.UUID,
        action: Action,
        effect: AccessFactEffect,
    ) -> AccessFact | None:
        """Look up access fact by natural key. Silent — no event emitted.

        Used as idempotency refetch after DuplicateAccessFactError.
        account_id=None is handled with IS NULL predicate (NULLS NOT DISTINCT).
        """
        return await repo_get_access_fact_by_natural_key(
            session,
            subject_id=subject_id,
            account_id=account_id,
            resource_id=resource_id,
            action=action,
            effect=effect,
        )

    async def get_fact(
        self,
        session: AsyncSession,
        fact_id: uuid.UUID,
    ) -> AccessFact | None:
        """Get access fact by id. Emits access_fact.retrieved when found."""
        fact = await repo_get_access_fact_by_id(session, fact_id)
        if fact is not None:
            self._log.emit_safe(
                'access_fact.retrieved',
                LogLevel.INFO,
                'Access fact retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {'access_fact_id': str(fact_id)},
                    actor_component=_COMPONENT,
                    target_id='access_fact',
                ),
            )
        return fact

    async def list_facts(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        action: Action | None = None,
        effect: AccessFactEffect | None = None,
        valid_at: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AccessFact]:
        """List access facts with optional filters. No event emitted."""
        return await repo_list_access_facts(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=account_id,
            action=action,
            effect=effect,
            valid_at=valid_at,
            limit=limit,
            offset=offset,
        )

    async def invalidate_fact(
        self,
        session: AsyncSession,
        fact_id: uuid.UUID,
        *,
        at: datetime | None = None,
    ) -> AccessFact:
        """Invalidate an access fact by setting valid_until. Emits access_fact.invalidated at WARNING."""
        fact = await repo_get_access_fact_by_id(session, fact_id)
        if fact is None:
            raise AccessFactNotFoundError(fact_id)

        ts = at or datetime.now(UTC)
        await repo_invalidate_access_fact(session, fact, at=ts)

        self._log.emit_safe(
            'access_fact.invalidated',
            LogLevel.WARNING,
            'Access fact invalidated',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'access_fact_id': str(fact_id),
                    'at': str(ts),
                },
                actor_component=_COMPONENT,
                target_id='access_fact',
            ),
        )
        return fact
