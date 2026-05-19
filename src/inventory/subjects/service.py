# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.subjects.models import Subject, SubjectAttribute, SubjectKind, SubjectNHIKind, SubjectStatus

if TYPE_CHECKING:
    from src.inventory.customers.models import Customer
    from src.inventory.employees.models import Employee
    from src.inventory.nhi.models import NHI
from src.inventory.subjects.repository import (
    bulk_upsert_employee_subjects as repo_bulk_upsert_employee_subjects,
)
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
    find_employee_subjects_excluding_keys as repo_find_employee_subjects_excluding_keys,
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
    resolve_employees_by_person_ids as repo_resolve_employees_by_person_ids,
)
from src.inventory.subjects.repository import (
    resolve_persons_by_external_ids as repo_resolve_persons_by_external_ids,
)
from src.inventory.subjects.repository import (
    update_subject as repo_update_subject,
)
from src.inventory.subjects.schemas import SubjectBulkItem, SubjectPatch, _check_status_for_kind
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

    def __init__(self, conflicts: list[tuple[str, str]] | None = None) -> None:
        self.conflicts = conflicts or []
        super().__init__('Employee already bound to a different Subject')


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


class UnknownPersonExternalIdsError(Exception):
    """Raised when one or more person_external_ids are not found in persons."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f'Unknown person_external_ids: {", ".join(missing)}')


class UnresolvedEmployeesForPersonsError(Exception):
    """Raised when persons exist but have no Employee row.

    A person can exist without an employee record (e.g. a contractor
    not yet promoted). For subject bulk upsert with kind=employee,
    every input person MUST have an employee — otherwise we cannot
    bind the Subject to a principal_employee_id.
    """

    def __init__(self, missing_person_external_ids: list[str]) -> None:
        self.missing = missing_person_external_ids
        super().__init__(f'No employee record for person_external_ids: {", ".join(missing_person_external_ids)}')


# ---------------------------------------------------------------------------
# Module-level helpers — validators, translators, envelope builders
# ---------------------------------------------------------------------------


def _validate_subject_status_for_kind(kind: SubjectKind, status: str) -> None:
    """Raise InvalidSubjectStatusForKindError if status is incompatible with kind."""
    try:
        _check_status_for_kind(kind, status)
    except ValueError as exc:
        raise InvalidSubjectStatusForKindError(kind, status) from exc


def _translate_subject_create_integrity_error(exc: IntegrityError) -> NoReturn:
    """Translate pgcode 23503/23505 to domain errors; re-raises original otherwise."""
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    if pgcode == '23503':
        raise SubjectPrincipalNotFoundError() from None
    if pgcode == '23505':
        raise SubjectPrincipalAlreadyBoundError() from None
    raise exc


def _build_subject_created_event(subject: Subject, correlation_id: str | None) -> EventEnvelope:
    """Build EventEnvelope for inventory.subject.created."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.subject.created',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'subject_id': str(subject.id),
            'kind': subject.kind.value,
            'nhi_kind': subject.nhi_kind.value if subject.nhi_kind else None,
            'principal_employee_id': (str(subject.principal_employee_id) if subject.principal_employee_id else None),
            'principal_nhi_id': (str(subject.principal_nhi_id) if subject.principal_nhi_id else None),
            'principal_customer_id': (str(subject.principal_customer_id) if subject.principal_customer_id else None),
            'status': subject.status,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(subject.id),
    )


def _build_subject_updated_event(
    subject_id: uuid.UUID,
    changed_fields: set[str] | frozenset[str] | list[str],
    correlation_id: str | None,
) -> EventEnvelope:
    """Build EventEnvelope for inventory.subject.updated."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.subject.updated',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'subject_id': str(subject_id),
            'changed_fields': sorted(changed_fields),
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(subject_id),
    )


def _build_subject_attribute_added_event(
    subject_id: uuid.UUID,
    key: str,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build EventEnvelope for inventory.subject.attribute_added."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.subject.attribute_added',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'subject_id': str(subject_id),
            'key': key,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(subject_id),
    )


def _build_subject_attribute_removed_event(
    subject_id: uuid.UUID,
    key: str,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build EventEnvelope for inventory.subject.attribute_removed."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.subject.attribute_removed',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'subject_id': str(subject_id),
            'key': key,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(subject_id),
    )


def _build_subject_status_changed_event(
    subject: Subject,
    previous_status: str,
    new_status: str,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build EventEnvelope for inventory.subject.status_changed."""
    return EventEnvelope(
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
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(subject.id),
    )


def _build_subject_bulk_upserted_event(
    subjects: list[Subject],
    correlation_id: str | None,
) -> EventEnvelope:
    """Build EventEnvelope for inventory.subject.bulk_upserted."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.subject.bulk_upserted',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'count': len(subjects),
            'kind': SubjectKind.employee.value,
            'external_ids': [s.external_id for s in subjects],
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=_COMPONENT,
    )


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
            _translate_subject_create_integrity_error(exc)

        await self._events.emit(_build_subject_created_event(subject, correlation_id))
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
        principal_employee_id: uuid.UUID | None = None,
        principal_nhi_id: uuid.UUID | None = None,
        principal_customer_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Subject], int]:
        """List subjects with optional filters. Returns (rows, total). No event emitted."""
        return await repo_list_subjects(
            session,
            kind=kind,
            status=status,
            principal_employee_id=principal_employee_id,
            principal_nhi_id=principal_nhi_id,
            principal_customer_id=principal_customer_id,
            limit=limit,
            offset=offset,
        )

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
            _validate_subject_status_for_kind(subject.kind, patch.status)

        changed_fields = await repo_update_subject(
            session,
            subject,
            status=patch.status,
        )
        if changed_fields:
            await self._events.emit(_build_subject_updated_event(subject_id, changed_fields, correlation_id))
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
        await self._events.emit(_build_subject_attribute_added_event(subject_id, key, correlation_id))
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
        await self._events.emit(_build_subject_attribute_removed_event(subject_id, key, correlation_id))

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
            _build_subject_status_changed_event(subject, previous_status, new_status, correlation_id)
        )
        return subject

    async def ensure_for_principal(
        self,
        session: AsyncSession,
        *,
        kind: SubjectKind,
        principal_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> Subject:
        """Return the Subject bound to (kind, principal_id), creating one if absent.

        Idempotent: if a Subject already references the principal via the
        matching principal_<kind>_id column, it is returned as-is. Otherwise
        a new row is inserted with external_id=uuid4().hex, status derived
        from the principal (Employee/NHI/Customer is_locked / email_verified
        rules — same logic as recompute_status_for_principal), and the
        appropriate principal_<kind>_id FK set.

        Emits ``inventory.subject.created`` IFF a new row was inserted. Does
        not emit on the return-existing branch.

        Does NOT commit. Caller owns the transaction boundary.

        Raises SubjectStatusRecomputePrincipalMissingError if the principal
        does not exist (same FK-integrity signal as recompute_status_for_principal).
        """
        subject = await repo_get_subject_by_principal(session, kind, principal_id)
        if subject is not None:
            return subject

        principal = await _load_principal(session, kind, principal_id)
        if principal is None:
            raise SubjectStatusRecomputePrincipalMissingError(
                uuid.UUID(int=0),
                kind,
                principal_id,
            )

        status: str = derive_subject_status(kind, principal)

        nhi_kind: SubjectNHIKind | None = None
        if kind == SubjectKind.nhi:
            nhi_kind = SubjectNHIKind.service_account

        principal_employee_id: uuid.UUID | None = None
        principal_nhi_id: uuid.UUID | None = None
        principal_customer_id: uuid.UUID | None = None
        if kind == SubjectKind.employee:
            principal_employee_id = principal_id
        elif kind == SubjectKind.nhi:
            principal_nhi_id = principal_id
        elif kind == SubjectKind.customer:
            principal_customer_id = principal_id

        subject = await self.create_subject(
            session,
            external_id=uuid.uuid4().hex,
            kind=kind,
            nhi_kind=nhi_kind,
            principal_employee_id=principal_employee_id,
            principal_nhi_id=principal_nhi_id,
            principal_customer_id=principal_customer_id,
            status=status,
            correlation_id=correlation_id,
        )
        return subject

    async def bulk_upsert_employee_subjects(
        self,
        session: AsyncSession,
        items: list[SubjectBulkItem],
        correlation_id: str | None = None,
    ) -> list[Subject]:
        """Bulk-upsert employee-kind subjects.

        Resolves person_external_id → person_id → employee_id, then
        upserts by (kind='employee', external_id). Emits
        inventory.subject.bulk_upserted.

        Raises:
            UnknownPersonExternalIdsError: any person_external_id absent
                from persons.
            UnresolvedEmployeesForPersonsError: a person exists but has
                no employee row.
            SubjectPrincipalAlreadyBoundError: an employee_id is already
                bound to a different Subject (partial-unique violation
                on uq_subjects_principal_employee_id). Translated from
                IntegrityError pgcode 23505.

        """
        person_external_ids = [item.person_external_id for item in items]
        person_map = await repo_resolve_persons_by_external_ids(session, person_external_ids)

        missing_persons = [eid for eid in person_external_ids if eid not in person_map]
        if missing_persons:
            raise UnknownPersonExternalIdsError(missing_persons)

        person_ids = [person_map[eid] for eid in person_external_ids]
        employee_map = await repo_resolve_employees_by_person_ids(
            session,
            list(set(person_ids)),
        )

        # Map each input item back through person_external_id -> person_id ->
        # employee_id. If any item's person has no employee row, collect the
        # original person_external_id for the error message.
        missing_employees: list[str] = []
        items_with_employee_ids: list[tuple[str, uuid.UUID, str]] = []
        for item in items:
            person_id = person_map[item.person_external_id]
            employee_id = employee_map.get(person_id)
            if employee_id is None:
                missing_employees.append(item.person_external_id)
                continue
            items_with_employee_ids.append((item.external_id, employee_id, item.status.value))

        if missing_employees:
            raise UnresolvedEmployeesForPersonsError(missing_employees)

        # Pre-SELECT: detect employee_ids already bound to OTHER subjects
        # (different external_id) so we can give a useful 409 message.
        incoming_employee_ids = [emp_id for _, emp_id, _ in items_with_employee_ids]
        incoming_ext_ids = {ext_id for ext_id, _, _ in items_with_employee_ids}
        conflict_subjects = await repo_find_employee_subjects_excluding_keys(
            session,
            employee_ids=incoming_employee_ids,
            exclude_external_ids=incoming_ext_ids,
        )
        if conflict_subjects:
            # Build useful error: map employee_id back to person_external_id
            emp_to_person_ext: dict[uuid.UUID, str] = {
                v: k
                for k, v in {
                    eid: employee_map[person_map[eid]]
                    for eid in person_external_ids
                    if person_map.get(eid) and employee_map.get(person_map[eid])
                }.items()
            }
            raise SubjectPrincipalAlreadyBoundError(
                conflicts=[
                    (
                        emp_to_person_ext.get(s.principal_employee_id, str(s.principal_employee_id)),
                        s.external_id,
                    )
                    for s in conflict_subjects
                    if s.principal_employee_id is not None
                ]
            )

        try:
            subjects = await repo_bulk_upsert_employee_subjects(session, items_with_employee_ids)
        except IntegrityError as exc:
            # Fallback for race-condition window: generic message.
            _translate_subject_create_integrity_error(exc)

        await self._events.emit(_build_subject_bulk_upserted_event(subjects, correlation_id))
        return subjects


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
