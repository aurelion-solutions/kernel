# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative service — business logic and event emission."""

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
    get_by_unique_key as repo_get_by_unique_key,
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
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

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
    """Orchestrates initiative CRUD and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_initiative(
        self,
        session: AsyncSession,
        *,
        access_fact_id: uuid.UUID,
        type_: InitiativeType,
        origin: str,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        correlation_id: str | None = None,
    ) -> Initiative:
        """Create an initiative. Emits inventory.initiative.created.

        Phase 15: ``access_facts`` was dropped from PG — facts now live in Iceberg
        ``normalized.access_facts``. ``Initiative.access_fact_id`` is a plain UUID
        with no FK constraint, so no existence check is performed here.
        """
        try:
            initiative = await repo_create_initiative(
                session,
                access_fact_id=access_fact_id,
                type_=type_,
                origin=origin,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        except IntegrityError:
            await session.rollback()
            raise

        created_payload = {
            'initiative_id': str(initiative.id),
            'access_fact_id': str(access_fact_id),
            'type': type_.value,
            'origin': origin,
            'valid_from': str(initiative.valid_from),
            'valid_until': str(initiative.valid_until) if initiative.valid_until is not None else None,
        }
        corr = correlation_id if correlation_id is not None else uuid.uuid4().hex
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.initiative.created',
                occurred_at=datetime.now(UTC),
                correlation_id=corr,
                causation_id=None,
                payload=created_payload,
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(initiative.id),
            )
        )
        # Also emit the canonical initiative.changed event consumed by the MQ matcher
        # (E3 routing key: inventory.initiative.changed).
        changed_payload = {
            'initiative_id': str(initiative.id),
            'access_fact_id': str(access_fact_id),
            'change_type': 'created',
        }
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.initiative.changed',
                occurred_at=datetime.now(UTC),
                correlation_id=corr,
                causation_id=None,
                payload=changed_payload,
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(initiative.id),
            )
        )
        return initiative

    async def get_initiative(
        self,
        session: AsyncSession,
        initiative_id: uuid.UUID,
    ) -> Initiative | None:
        """Get initiative by id. No event emitted (Q1 — retrieved signal dropped,
        audit deferred to future audit.* slice).
        """
        return await repo_get_initiative_by_id(session, initiative_id)

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
        correlation_id: str | None = None,
    ) -> Initiative:
        """Apply partial update to initiative.

        Emits inventory.initiative.updated when values actually change.
        Emits inventory.initiative.expired when valid_until transitions an active initiative into the past.
        """
        fields_set = payload.model_fields_set
        if not fields_set:
            raise InitiativeEmptyPatchError

        initiative = await repo_get_initiative_by_id(session, initiative_id)
        if initiative is None:
            raise InitiativeNotFoundError(initiative_id)

        patch_fields = payload.model_dump(exclude_unset=True)
        # Snapshot BEFORE repo_update flushes setattr mutations — all Initiative mutable fields are
        # immutable Python values (str/datetime/Enum), so identity-equal comparison via != is safe.
        previous_values = {field: getattr(initiative, field) for field in patch_fields}
        previous_valid_until = cast(datetime | None, initiative.valid_until)
        now_utc = datetime.now(UTC)

        initiative = await repo_update_initiative(session, initiative, fields=patch_fields)

        changed_fields = {field for field, prev in previous_values.items() if getattr(initiative, field) != prev}

        if changed_fields:
            upd_corr = correlation_id if correlation_id is not None else uuid.uuid4().hex
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='inventory.initiative.updated',
                    occurred_at=datetime.now(UTC),
                    correlation_id=upd_corr,
                    causation_id=None,
                    payload={
                        'initiative_id': str(initiative_id),
                        'access_fact_id': str(initiative.access_fact_id),
                        'changed_fields': sorted(changed_fields),
                    },
                    actor_kind=EventParticipantKind.COMPONENT,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=str(initiative.id),
                )
            )
            # Canonical initiative.changed event for MQ matcher (E3).
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='inventory.initiative.changed',
                    occurred_at=datetime.now(UTC),
                    correlation_id=upd_corr,
                    causation_id=None,
                    payload={
                        'initiative_id': str(initiative_id),
                        'access_fact_id': str(initiative.access_fact_id),
                        'change_type': 'updated',
                    },
                    actor_kind=EventParticipantKind.COMPONENT,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=str(initiative.id),
                )
            )

        if 'valid_until' in patch_fields:
            new_valid_until = cast(datetime | None, initiative.valid_until)
            was_active = previous_valid_until is None or previous_valid_until > now_utc
            is_expired = new_valid_until is not None and new_valid_until <= now_utc
            if was_active and is_expired:
                await self._events.emit(
                    EventEnvelope(
                        event_id=uuid.uuid4(),
                        event_type='inventory.initiative.expired',
                        occurred_at=datetime.now(UTC),
                        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                        causation_id=None,
                        payload={
                            'initiative_id': str(initiative_id),
                            'access_fact_id': str(initiative.access_fact_id),
                            'at': str(new_valid_until),
                        },
                        actor_kind=EventParticipantKind.COMPONENT,
                        actor_id=_COMPONENT,
                        target_kind=EventParticipantKind.SYSTEM,
                        target_id=str(initiative.id),
                    )
                )

        return initiative

    async def create_or_get(
        self,
        session: AsyncSession,
        *,
        access_fact_id: uuid.UUID,
        type_: InitiativeType,
        origin: str,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        subject_ref: str | None = None,
        subject_type: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[Initiative, bool]:
        """Idempotent upsert by (access_fact_id, type, origin).

        Returns (initiative, created) where created=True means a new row was inserted,
        created=False means an existing row was returned unchanged.

        Used by the F3 grant-path chain in access_apply.  The caller (execute_plan)
        is safe to call this multiple times on crash-recovery restart — the second
        invocation returns the existing initiative without emitting a duplicate event.
        """
        existing = await repo_get_by_unique_key(
            session,
            access_fact_id=access_fact_id,
            type_=type_,
            origin=origin,
        )
        if existing is not None:
            return existing, False

        initiative = await repo_create_initiative(
            session,
            access_fact_id=access_fact_id,
            type_=type_,
            origin=origin,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        if subject_ref is not None:
            initiative.subject_ref = subject_ref
        if subject_type is not None:
            initiative.subject_type = subject_type
        await session.flush()

        corr = correlation_id if correlation_id is not None else uuid.uuid4().hex
        created_payload = {
            'initiative_id': str(initiative.id),
            'access_fact_id': str(access_fact_id),
            'type': type_.value,
            'origin': origin,
            'valid_from': str(initiative.valid_from),
            'valid_until': str(initiative.valid_until) if initiative.valid_until is not None else None,
        }
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.initiative.created',
                occurred_at=datetime.now(UTC),
                correlation_id=corr,
                causation_id=None,
                payload=created_payload,
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(initiative.id),
            )
        )
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.initiative.changed',
                occurred_at=datetime.now(UTC),
                correlation_id=corr,
                causation_id=None,
                payload={
                    'initiative_id': str(initiative.id),
                    'access_fact_id': str(access_fact_id),
                    'change_type': 'created',
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(initiative.id),
            )
        )
        return initiative, True

    async def close(
        self,
        session: AsyncSession,
        initiative_id: uuid.UUID,
        *,
        valid_until: datetime | None = None,
        correlation_id: str | None = None,
    ) -> Initiative:
        """Close an initiative by setting valid_until to now (or provided timestamp).

        Idempotent: if valid_until is already <= now(), the UPDATE is a no-op with
        respect to observable state.  The initiative row is NEVER deleted — audit
        trail must remain intact.

        Emits inventory.initiative.expired if the initiative transitions from active
        to closed.
        """
        initiative = await repo_get_initiative_by_id(session, initiative_id)
        if initiative is None:
            raise InitiativeNotFoundError(initiative_id)

        close_at = valid_until if valid_until is not None else datetime.now(UTC)
        now_utc = datetime.now(UTC)

        previous_valid_until = cast(datetime | None, initiative.valid_until)
        was_active = previous_valid_until is None or previous_valid_until > now_utc

        # Idempotent: already closed — nothing to do.
        if not was_active:
            return initiative

        initiative = await repo_update_initiative(
            session,
            initiative,
            fields={'valid_until': close_at},
        )

        corr = correlation_id if correlation_id is not None else uuid.uuid4().hex
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.initiative.expired',
                occurred_at=datetime.now(UTC),
                correlation_id=corr,
                causation_id=None,
                payload={
                    'initiative_id': str(initiative_id),
                    'access_fact_id': str(initiative.access_fact_id),
                    'at': str(close_at),
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(initiative.id),
            )
        )
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.initiative.changed',
                occurred_at=datetime.now(UTC),
                correlation_id=corr,
                causation_id=None,
                payload={
                    'initiative_id': str(initiative_id),
                    'access_fact_id': str(initiative.access_fact_id),
                    'change_type': 'closed',
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(initiative.id),
            )
        )
        return initiative
