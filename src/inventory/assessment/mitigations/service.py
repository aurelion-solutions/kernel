# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationService — business logic for the Mitigation slice.

Events are emitted on ``aurelion.events`` (domain bus), not the log bus.
Logs are emitted on LogService for operational anomalies only.

Allowed status transitions:
  proposed → active    (emits mitigation.activated)
  proposed → revoked   (emits mitigation.revoked; reason required)
  active   → revoked   (emits mitigation.revoked; reason required)

expired is terminal and unreachable via API (reserved for the expiry sweep).
revoked is terminal.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.mitigations.exceptions import (
    MitigationControlInactiveError,
    MitigationControlNotFoundError,
    MitigationDuplicateActiveError,
    MitigationInvalidInitialStatusError,
    MitigationNotFoundError,
    MitigationOwnerNotFoundError,
    MitigationReasonRequiredError,
    MitigationRuleNotFoundError,
    MitigationRuleNotMitigatableError,
    MitigationScopePairError,
    MitigationStatusTransitionError,
    MitigationSubjectNotFoundError,
    MitigationValidWindowError,
)
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus
from src.inventory.assessment.mitigations.repository import (
    get_mitigation_by_id,
    get_mitigation_control_is_active,
    get_sod_rule_mitigation_allowed,
    insert_mitigation,
    list_mitigations,
    subject_exists,
    update_mitigation_status_fields,
)
from src.inventory.assessment.mitigations.schemas import (
    MitigationCreate,
    MitigationRead,
    MitigationStatusPatch,
)
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService

_COMPONENT = 'access_analysis.mitigations'

# Valid initial statuses for create
_VALID_INITIAL_STATUSES = {MitigationStatus.proposed, MitigationStatus.active}

# Valid target statuses via PATCH (expired is sweep-only)
_ALLOWED_TRANSITIONS: dict[MitigationStatus, set[MitigationStatus]] = {
    MitigationStatus.proposed: {MitigationStatus.active, MitigationStatus.revoked},
    MitigationStatus.active: {MitigationStatus.revoked},
    MitigationStatus.expired: set(),
    MitigationStatus.revoked: set(),
}


def _build_mitigation_created_event(
    mitigation: Mitigation,
    correlation_id: str,
    actor_id: str,
    actor_kind: EventParticipantKind,
) -> EventEnvelope:
    """Build the access_analysis.mitigation.created EventEnvelope."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='access_analysis.mitigation.created',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'mitigation_id': str(mitigation.id),
            'rule_id': str(mitigation.rule_id),
            'control_id': str(mitigation.control_id),
            'subject_id': str(mitigation.subject_id),
            'scope_key_id': str(mitigation.scope_key_id) if mitigation.scope_key_id is not None else None,
            'scope_value': mitigation.scope_value,
            'status': mitigation.status,
            'valid_from': mitigation.valid_from.isoformat(),
            'valid_until': mitigation.valid_until.isoformat() if mitigation.valid_until else None,
            'owner_id': str(mitigation.owner_id),
            'level': 'INFO',
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(mitigation.id),
    )


def _build_mitigation_activated_event(
    mitigation: Mitigation,
    correlation_id: str,
    actor_id: str,
    actor_kind: EventParticipantKind,
) -> EventEnvelope:
    """Build the access_analysis.mitigation.activated EventEnvelope."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='access_analysis.mitigation.activated',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'mitigation_id': str(mitigation.id),
            'rule_id': str(mitigation.rule_id),
            'control_id': str(mitigation.control_id),
            'subject_id': str(mitigation.subject_id),
            'scope_key_id': str(mitigation.scope_key_id) if mitigation.scope_key_id is not None else None,
            'scope_value': mitigation.scope_value,
            'status': mitigation.status,
            'valid_from': mitigation.valid_from.isoformat(),
            'valid_until': mitigation.valid_until.isoformat() if mitigation.valid_until else None,
            'owner_id': str(mitigation.owner_id),
            'level': 'INFO',
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(mitigation.id),
    )


def _build_mitigation_revoked_event(
    mitigation: Mitigation,
    correlation_id: str,
    actor_id: str,
    actor_kind: EventParticipantKind,
) -> EventEnvelope:
    """Build the access_analysis.mitigation.revoked EventEnvelope."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='access_analysis.mitigation.revoked',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'mitigation_id': str(mitigation.id),
            'rule_id': str(mitigation.rule_id),
            'control_id': str(mitigation.control_id),
            'subject_id': str(mitigation.subject_id),
            'scope_key_id': str(mitigation.scope_key_id) if mitigation.scope_key_id is not None else None,
            'scope_value': mitigation.scope_value,
            'status': mitigation.status,
            'valid_from': mitigation.valid_from.isoformat(),
            'valid_until': mitigation.valid_until.isoformat() if mitigation.valid_until else None,
            'owner_id': str(mitigation.owner_id),
            'reason': mitigation.reason,
            'level': 'WARNING',
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(mitigation.id),
    )


def _resolve_actor(created_by: str | None) -> tuple[EventParticipantKind, str]:
    """Resolve actor kind and id from created_by field."""
    if created_by:
        return EventParticipantKind.USER, created_by
    return EventParticipantKind.COMPONENT, _COMPONENT


async def _validate_create_invariants(
    session: AsyncSession,
    payload: MitigationCreate,
) -> None:
    """Validate all business invariants for create. Raises on first violation."""
    if payload.status not in _VALID_INITIAL_STATUSES:
        raise MitigationInvalidInitialStatusError(payload.status)

    mitigation_allowed = await get_sod_rule_mitigation_allowed(session, payload.rule_id)
    if mitigation_allowed is None:
        raise MitigationRuleNotFoundError(payload.rule_id)
    if not mitigation_allowed:
        raise MitigationRuleNotMitigatableError(payload.rule_id)

    control_is_active = await get_mitigation_control_is_active(session, payload.control_id)
    if control_is_active is None:
        raise MitigationControlNotFoundError(payload.control_id)
    if not control_is_active:
        raise MitigationControlInactiveError(payload.control_id)

    if not await subject_exists(session, payload.subject_id):
        raise MitigationSubjectNotFoundError(payload.subject_id)

    if not await subject_exists(session, payload.owner_id):
        raise MitigationOwnerNotFoundError(payload.owner_id)

    scope_key_set = payload.scope_key_id is not None
    scope_value_set = payload.scope_value is not None
    if scope_key_set != scope_value_set:
        raise MitigationScopePairError()

    if payload.valid_until is not None and payload.valid_until <= payload.valid_from:
        raise MitigationValidWindowError()


def _validate_status_transition(
    current: MitigationStatus,
    requested: MitigationStatus,
) -> None:
    """Raise MitigationStatusTransitionError if the transition is not allowed."""
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if requested not in allowed:
        raise MitigationStatusTransitionError(current, requested)


def _translate_insert_integrity_error(
    exc: IntegrityError,
    log_service: LogService,
    correlation_id: str,
) -> None:
    """Translate IntegrityError from insert into a domain error, or re-raise.

    asyncpg wraps the original error as exc.orig.__cause__; constraint_name lives there.
    Emits a WARNING log if the integrity error is unexpected.
    """
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint_name: str | None = getattr(asyncpg_exc, 'constraint_name', None)
    if pgcode == '23505' and constraint_name == 'uq_mitigations_active_or_proposed':
        raise MitigationDuplicateActiveError() from None
    log_service.emit_safe(
        level=LogLevel.WARNING,
        message=f'Unexpected IntegrityError during mitigation insert: {exc}',
        component=_COMPONENT,
        payload={'constraint': constraint_name, 'pgcode': pgcode},
        correlation_id=correlation_id,
    )
    raise exc


class MitigationService:
    """Service for Mitigation lifecycle: create + status transitions.

    Depends on both LogService (operational) and EventService (domain events).
    Emits domain events post-flush, pre-commit within the same session.
    """

    def __init__(
        self,
        session: AsyncSession,
        log_service: LogService,
        event_service: EventService,
    ) -> None:
        self._session = session
        self._logs = log_service
        self._events = event_service

    async def create(
        self,
        payload: MitigationCreate,
        *,
        correlation_id: str | None = None,
    ) -> MitigationRead:
        """Create a new Mitigation.

        Initial status may be 'proposed' or 'active' only.
        If created as 'active', emits mitigation.created only (not mitigation.activated).
        """
        cid = correlation_id or uuid.uuid4().hex
        await _validate_create_invariants(self._session, payload)

        try:
            mitigation = await insert_mitigation(
                self._session,
                rule_id=payload.rule_id,
                control_id=payload.control_id,
                subject_id=payload.subject_id,
                scope_key_id=payload.scope_key_id,
                scope_value=payload.scope_value,
                reason=payload.reason,
                status=payload.status,
                valid_from=payload.valid_from,
                valid_until=payload.valid_until,
                owner_id=payload.owner_id,
                created_by=payload.created_by,
            )
        except IntegrityError as exc:
            _translate_insert_integrity_error(exc, self._logs, cid)
            raise  # unreachable; keeps type checker happy

        actor_kind, actor_id = _resolve_actor(payload.created_by)
        event = _build_mitigation_created_event(mitigation, cid, actor_id, actor_kind)
        await self._events.emit(event)

        return MitigationRead.model_validate(mitigation)

    async def patch_status(
        self,
        mitigation_id: int,
        payload: MitigationStatusPatch,
        *,
        correlation_id: str | None = None,
    ) -> MitigationRead:
        """Apply a status transition to an existing Mitigation.

        Supported transitions:
          proposed → active    (emits mitigation.activated)
          proposed → revoked   (reason required; emits mitigation.revoked)
          active   → revoked   (reason required; emits mitigation.revoked)

        expired is not a valid PATCH target — reserved for the expiry sweep.
        """
        cid = correlation_id or uuid.uuid4().hex
        mitigation = await get_mitigation_by_id(self._session, mitigation_id)
        if mitigation is None:
            raise MitigationNotFoundError(mitigation_id)

        _validate_status_transition(mitigation.status, payload.status)

        if payload.status == MitigationStatus.revoked and not payload.reason:
            raise MitigationReasonRequiredError()

        mitigation = await update_mitigation_status_fields(
            self._session,
            mitigation,
            status=payload.status,
            reason=payload.reason if payload.status == MitigationStatus.revoked else None,
        )

        actor_kind, actor_id = _resolve_actor(None)
        if payload.status == MitigationStatus.active:
            event = _build_mitigation_activated_event(mitigation, cid, actor_id, actor_kind)
        else:
            event = _build_mitigation_revoked_event(mitigation, cid, actor_id, actor_kind)
        await self._events.emit(event)

        return MitigationRead.model_validate(mitigation)

    async def get(self, mitigation_id: int) -> MitigationRead:
        """Return a Mitigation by id. Raises MitigationNotFoundError when missing."""
        mitigation = await get_mitigation_by_id(self._session, mitigation_id)
        if mitigation is None:
            raise MitigationNotFoundError(mitigation_id)
        return MitigationRead.model_validate(mitigation)

    async def list(
        self,
        *,
        rule_id: int | None = None,
        subject_id: uuid.UUID | None = None,
        status: MitigationStatus | None = None,
        control_id: int | None = None,
        owner_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MitigationRead]:
        """Return mitigations, optionally filtered."""
        rows = await list_mitigations(
            self._session,
            rule_id=rule_id,
            subject_id=subject_id,
            status=status,
            control_id=control_id,
            owner_id=owner_id,
            limit=limit,
            offset=offset,
        )
        return [MitigationRead.model_validate(row) for row in rows]
