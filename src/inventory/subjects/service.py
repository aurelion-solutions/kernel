# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.subjects.models import Subject, SubjectAttribute, SubjectKind, SubjectNHIKind, SubjectStatus

if TYPE_CHECKING:
    from src.inventory.customers.models import Customer
    from src.inventory.employees.models import Employee
    from src.inventory.nhi.models import NHI
from src.inventory.subjects.repository import (
    create_subject as repo_create_subject,
)
from src.inventory.subjects.repository import (
    create_subject_attribute as repo_create_subject_attribute,
)
from src.inventory.subjects.repository import (
    delete_subject_attribute as repo_delete_subject_attribute,
)
from src.inventory.subjects.repository import (
    get_subject_by_id as repo_get_subject_by_id,
)
from src.inventory.subjects.repository import (
    get_subject_by_principal as repo_get_subject_by_principal,
)
from src.inventory.subjects.repository import (
    list_subject_attributes as repo_list_subject_attributes,
)
from src.inventory.subjects.repository import (
    list_subjects as repo_list_subjects,
)
from src.inventory.subjects.repository import (
    update_subject as repo_update_subject,
)
from src.inventory.subjects.schemas import SubjectPatch, _check_status_for_kind
from src.inventory.subjects.status_derivation import derive_subject_status
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.subjects'


class SubjectNotFoundError(Exception):
    """Raised when a subject is not found."""

    def __init__(self, subject_id: uuid.UUID) -> None:
        self.subject_id = subject_id
        super().__init__(f'Subject not found: {subject_id}')


class SubjectPrincipalNotFoundError(Exception):
    """Raised when the referenced principal entity does not exist (FK violation, pgcode 23503)."""

    def __init__(self, subject_id: uuid.UUID | None = None) -> None:
        self.subject_id = subject_id
        super().__init__('Referenced principal entity does not exist')


class SubjectPrincipalAlreadyBoundError(Exception):
    """Raised when the principal is already bound to another Subject (unique violation, pgcode 23505)."""

    def __init__(self) -> None:
        super().__init__('Principal is already bound to a Subject')


class InvalidSubjectStatusForKindError(Exception):
    """Raised when a status value is invalid for the subject's kind."""

    def __init__(self, kind: SubjectKind, status: str) -> None:
        self.kind = kind
        self.status = status
        super().__init__(f"status '{status}' is not valid for kind '{kind}'")


class DuplicateSubjectAttributeError(Exception):
    """Raised when adding an attribute with a key that already exists for the subject."""

    def __init__(self, subject_id: uuid.UUID, key: str) -> None:
        self.subject_id = subject_id
        self.key = key
        super().__init__(f'Duplicate attribute key for subject: {key}')


class SubjectAttributeNotFoundError(Exception):
    """Raised when a subject attribute is not found."""

    def __init__(self, subject_id: uuid.UUID, key: str) -> None:
        self.subject_id = subject_id
        self.key = key
        super().__init__(f'Subject attribute not found: {subject_id} / {key}')


class SubjectStatusRecomputePrincipalMissingError(Exception):
    """Raised when Subject exists but its principal row is missing — FK-integrity bug."""

    def __init__(
        self,
        subject_id: uuid.UUID,
        kind: SubjectKind,
        principal_id: uuid.UUID,
    ) -> None:
        self.subject_id = subject_id
        self.kind = kind
        self.principal_id = principal_id
        super().__init__(f'Subject {subject_id} references {kind} principal {principal_id} which does not exist')


class SubjectService:
    """Orchestrates subject CRUD and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_subject(
        self,
        session: AsyncSession,
        *,
        external_id: str,
        kind: SubjectKind,
        nhi_kind: SubjectNHIKind | None = None,
        principal_employee_id: uuid.UUID | None = None,
        principal_nhi_id: uuid.UUID | None = None,
        principal_customer_id: uuid.UUID | None = None,
        status: str,
        correlation_id: str | None = None,
    ) -> Subject:
        """Create a subject. Emits inventory.subject.created. Distinguishes FK vs unique violations."""
        try:
            subject = await repo_create_subject(
                session,
                external_id=external_id,
                kind=kind,
                nhi_kind=nhi_kind,
                principal_employee_id=principal_employee_id,
                principal_nhi_id=principal_nhi_id,
                principal_customer_id=principal_customer_id,
                status=status,
            )
        except IntegrityError as exc:
            # Discriminate FK violation (23503) from unique violation (23505).
            # With asyncpg the original exception is an asyncpg error; pgcode lives on it.
            orig = exc.orig
            pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
            if pgcode == '23503':
                raise SubjectPrincipalNotFoundError() from None
            if pgcode == '23505':
                raise SubjectPrincipalAlreadyBoundError() from None
            raise

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.subject.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'subject_id': str(subject.id),
                    'kind': subject.kind.value,
                    'nhi_kind': subject.nhi_kind.value if subject.nhi_kind else None,
                    'principal_employee_id': (
                        str(subject.principal_employee_id) if subject.principal_employee_id else None
                    ),
                    'principal_nhi_id': (str(subject.principal_nhi_id) if subject.principal_nhi_id else None),
                    'principal_customer_id': (
                        str(subject.principal_customer_id) if subject.principal_customer_id else None
                    ),
                    'status': subject.status,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(subject.id),
            )
        )
        return subject

    async def get_subject(
        self,
        session: AsyncSession,
        subject_id: uuid.UUID,
    ) -> Subject | None:
        """Get subject by id. No event emitted (Q1 — subject.retrieved dropped)."""
        return await repo_get_subject_by_id(session, subject_id)

    async def list_subjects(
        self,
        session: AsyncSession,
        *,
        kind: SubjectKind | None = None,
        status: SubjectStatus | None = None,
    ) -> list[Subject]:
        """List subjects. No event emitted."""
        return await repo_list_subjects(session, kind=kind, status=status)

    async def update_subject(
        self,
        session: AsyncSession,
        subject_id: uuid.UUID,
        patch: SubjectPatch,
        correlation_id: str | None = None,
    ) -> Subject:
        """Apply partial update to subject. Per-kind status validation. Emits inventory.subject.updated."""
        subject = await repo_get_subject_by_id(session, subject_id)
        if subject is None:
            raise SubjectNotFoundError(subject_id)

        if patch.status is not None:
            try:
                _check_status_for_kind(subject.kind, patch.status)
            except ValueError as exc:
                raise InvalidSubjectStatusForKindError(subject.kind, patch.status) from exc

        changed_fields = await repo_update_subject(
            session,
            subject,
            status=patch.status,
        )
        if changed_fields:
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='inventory.subject.updated',
                    occurred_at=datetime.now(UTC),
                    correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                    causation_id=None,
                    payload={
                        'subject_id': str(subject_id),
                        'changed_fields': sorted(changed_fields),
                    },
                    actor_kind=EventParticipantKind.CAPABILITY,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=str(subject_id),
                )
            )
        return subject

    async def list_attributes(
        self,
        session: AsyncSession,
        subject_id: uuid.UUID,
    ) -> list[SubjectAttribute]:
        """List attributes for a subject. Raises SubjectNotFoundError if missing."""
        subject = await repo_get_subject_by_id(session, subject_id)
        if subject is None:
            raise SubjectNotFoundError(subject_id)
        return await repo_list_subject_attributes(session, subject_id)

    async def add_attribute(
        self,
        session: AsyncSession,
        subject_id: uuid.UUID,
        key: str,
        value: str,
        correlation_id: str | None = None,
    ) -> SubjectAttribute:
        """Add attribute to subject. Emits inventory.subject.attribute_added. Raises on duplicate."""
        subject = await repo_get_subject_by_id(session, subject_id)
        if subject is None:
            raise SubjectNotFoundError(subject_id)
        try:
            attr = await repo_create_subject_attribute(
                session,
                subject_id=subject_id,
                key=key,
                value=value,
            )
        except IntegrityError:
            raise DuplicateSubjectAttributeError(subject_id, key) from None
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.subject.attribute_added',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'subject_id': str(subject_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(subject_id),
            )
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        subject_id: uuid.UUID,
        key: str,
        correlation_id: str | None = None,
    ) -> None:
        """Remove attribute from subject. Emits inventory.subject.attribute_removed. Raises if missing."""
        subject = await repo_get_subject_by_id(session, subject_id)
        if subject is None:
            raise SubjectNotFoundError(subject_id)
        deleted = await repo_delete_subject_attribute(session, subject_id, key)
        if not deleted:
            raise SubjectAttributeNotFoundError(subject_id, key)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.subject.attribute_removed',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'subject_id': str(subject_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(subject_id),
            )
        )

    async def recompute_status_for_principal(
        self,
        session: AsyncSession,
        *,
        kind: SubjectKind,
        principal_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> Subject | None:
        """Recompute Subject.status for the Subject bound to the given principal.

        Returns the Subject (updated or unchanged) or None if no Subject is bound
        (orphan principal — legitimate, not an error).

        Emits inventory.subject.status_changed iff the derived status differs from the stored one.
        Does NOT commit — caller owns the transaction.
        """
        subject = await repo_get_subject_by_principal(session, kind, principal_id)
        if subject is None:
            return None

        # Load principal to derive new status.
        principal = await _load_principal(session, kind, principal_id)
        if principal is None:
            raise SubjectStatusRecomputePrincipalMissingError(subject.id, kind, principal_id)

        new_status: str = derive_subject_status(kind, principal)

        if new_status == subject.status:
            return subject

        previous_status: str = subject.status
        subject.status = new_status
        await session.flush()

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.subject.status_changed',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'subject_id': str(subject.id),
                    'previous_status': previous_status,
                    'new_status': new_status,
                    'at': datetime.now(UTC).isoformat(),
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(subject.id),
            )
        )
        return subject


async def _load_principal(
    session: AsyncSession,
    kind: SubjectKind,
    principal_id: uuid.UUID,
) -> Customer | Employee | NHI | None:
    """Load the principal model row by kind. Returns None if not found."""
    from src.inventory.customers.models import Customer
    from src.inventory.employees.models import Employee
    from src.inventory.nhi.models import NHI

    if kind == SubjectKind.customer:
        return await session.get(Customer, principal_id)
    if kind == SubjectKind.employee:
        return await session.get(Employee, principal_id)
    if kind == SubjectKind.nhi:
        return await session.get(NHI, principal_id)
    raise ValueError(f'Unknown SubjectKind: {kind!r}')
