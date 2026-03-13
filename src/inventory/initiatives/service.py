# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative service — business logic and operational log emission."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.initiatives.repository import (
    create_initiative as repo_create_initiative,
)
from src.inventory.initiatives.repository import (
    get_initiative_by_id as repo_get_initiative_by_id,
)
from src.inventory.initiatives.repository import (
    list_initiatives as repo_list_initiatives,
)
from src.inventory.initiatives.repository import (
    update_initiative as repo_update_initiative,
)
from src.inventory.initiatives.schemas import InitiativePatch
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

_COMPONENT = 'inventory.initiatives'


class InitiativeNotFoundError(Exception):
    """Raised when an initiative is not found by id."""

    def __init__(self, initiative_id: uuid.UUID) -> None:
        self.initiative_id = initiative_id
        super().__init__(f'Initiative not found: {initiative_id}')


class InitiativeForeignKeyError(Exception):
    """Raised when the referenced access_fact_id is not found or FK constraint fails."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class InitiativeEmptyPatchError(Exception):
    """Raised when PATCH body contains no fields."""


class InitiativeService:
    """Orchestrates initiative creation, retrieval, and operational log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_initiative(
        self,
        session: AsyncSession,
        *,
        access_fact_id: uuid.UUID,
        type_: InitiativeType,
        origin: str,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> Initiative:
        """Create an initiative. Validates FK existence before insert."""
        from src.inventory.access_facts.models import AccessFact

        fact = await session.get(AccessFact, access_fact_id)
        if fact is None:
            raise InitiativeForeignKeyError(f'Access fact not found: {access_fact_id}')

        try:
            initiative = await repo_create_initiative(
                session,
                access_fact_id=access_fact_id,
                type_=type_,
                origin=origin,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        except IntegrityError as exc:
            await session.rollback()
            pgcode = getattr(exc.orig, 'pgcode', None) or getattr(exc.orig, 'sqlstate', None)
            if pgcode == '23503':
                raise InitiativeForeignKeyError(f'Access fact not found: {access_fact_id}') from exc
            raise

        self._log.emit_safe(
            'initiative.created',
            LogLevel.INFO,
            'Initiative created',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'initiative_id': str(initiative.id),
                    'access_fact_id': str(access_fact_id),
                    'type': type_.value,
                    'origin': origin,
                    'valid_from': str(initiative.valid_from),
                    'valid_until': str(initiative.valid_until) if initiative.valid_until is not None else None,
                },
                actor_component=_COMPONENT,
                target_id='initiative',
            ),
        )
        return initiative

    async def get_initiative(
        self,
        session: AsyncSession,
        initiative_id: uuid.UUID,
    ) -> Initiative | None:
        """Get initiative by id. Logs retrieval when found."""
        initiative = await repo_get_initiative_by_id(session, initiative_id)
        if initiative is not None:
            self._log.emit_safe(
                'initiative.retrieved',
                LogLevel.INFO,
                'Initiative retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {'initiative_id': str(initiative_id)},
                    actor_component=_COMPONENT,
                    target_id='initiative',
                ),
            )
        return initiative

    async def list_initiatives(
        self,
        session: AsyncSession,
        *,
        access_fact_id: uuid.UUID | None = None,
        type_: InitiativeType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Initiative]:
        """List initiatives with optional filters. No event emitted."""
        return await repo_list_initiatives(
            session,
            access_fact_id=access_fact_id,
            type_=type_,
            limit=limit,
            offset=offset,
        )

    async def update_initiative(
        self,
        session: AsyncSession,
        initiative_id: uuid.UUID,
        payload: InitiativePatch,
    ) -> Initiative:
        """Partially update an initiative. Emits updated and optionally expired events."""
        fields_set = payload.model_fields_set
        if not fields_set:
            raise InitiativeEmptyPatchError

        initiative = await repo_get_initiative_by_id(session, initiative_id)
        if initiative is None:
            raise InitiativeNotFoundError(initiative_id)

        now_utc = datetime.now(UTC)
        previous_valid_until = cast(datetime | None, initiative.valid_until)

        changes = payload.model_dump(exclude_unset=True)
        initiative = await repo_update_initiative(session, initiative, fields=changes)

        self._log.emit_safe(
            'initiative.updated',
            LogLevel.INFO,
            'Initiative updated',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'initiative_id': str(initiative_id),
                    'changed_fields': sorted(list(changes.keys())),
                },
                actor_component=_COMPONENT,
                target_id='initiative',
            ),
        )

        if 'valid_until' in changes:
            new_valid_until = cast(datetime | None, initiative.valid_until)
            was_active = previous_valid_until is None or previous_valid_until > now_utc
            is_expired = new_valid_until is not None and new_valid_until <= now_utc
            if was_active and is_expired:
                self._log.emit_safe(
                    'initiative.expired',
                    LogLevel.WARNING,
                    'Initiative expired',
                    _COMPONENT,
                    merge_emit_log_participant_fields(
                        {
                            'initiative_id': str(initiative_id),
                            'at': str(new_valid_until),
                        },
                        actor_component=_COMPONENT,
                        target_id='initiative',
                    ),
                )

        return initiative
