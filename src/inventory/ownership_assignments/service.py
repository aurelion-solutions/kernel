# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OwnershipAssignment service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.ownership_assignments.models import OwnershipAssignment, OwnershipKind
from src.inventory.ownership_assignments.repository import (
    create_ownership_assignment as repo_create,
)
from src.inventory.ownership_assignments.repository import (
    delete_ownership_assignment as repo_delete,
)
from src.inventory.ownership_assignments.repository import (
    get_ownership_assignment_by_id as repo_get_by_id,
)
from src.inventory.ownership_assignments.repository import (
    list_ownership_assignments as repo_list,
)
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.ownership_assignments'


class OwnershipAssignmentNotFoundError(Exception):
    """Raised when assignment is not found."""

    def __init__(self, assignment_id: uuid.UUID) -> None:
        self.assignment_id = assignment_id
        super().__init__(f'Ownership assignment not found: {assignment_id}')


class OwnershipAssignmentForeignKeyError(Exception):
    """Raised when a referenced entity (subject/resource/account) is not found."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class OwnershipAssignmentTargetRequiredError(Exception):
    """Raised when XOR rule is violated (both null or both set)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class OwnershipAssignmentDuplicateError(Exception):
    """Raised when unique constraint is violated (same subject/target/kind)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class OwnershipAssignmentService:
    """Orchestrates ownership assignment operations and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_assignment(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        kind: OwnershipKind,
        correlation_id: str | None = None,
    ) -> OwnershipAssignment:
        """Create an ownership assignment and emit inventory.ownership_assignment.created."""
        if (resource_id is None) == (account_id is None):
            raise OwnershipAssignmentTargetRequiredError('Exactly one of resource_id or account_id must be provided')

        from src.inventory.subjects.models import Subject

        subject = await session.get(Subject, subject_id)
        if subject is None:
            raise OwnershipAssignmentForeignKeyError(f'Subject not found: {subject_id}')

        if resource_id is not None:
            from src.inventory.resources.models import Resource

            resource = await session.get(Resource, resource_id)
            if resource is None:
                raise OwnershipAssignmentForeignKeyError(f'Resource not found: {resource_id}')

        if account_id is not None:
            from src.inventory.accounts.models import Account

            account = await session.get(Account, account_id)
            if account is None:
                raise OwnershipAssignmentForeignKeyError(f'Account not found: {account_id}')

        try:
            assignment = await repo_create(
                session,
                subject_id=subject_id,
                resource_id=resource_id,
                account_id=account_id,
                kind=kind,
            )
        except IntegrityError as exc:
            await session.rollback()
            pgcode = getattr(exc.orig, 'pgcode', None) or getattr(exc.orig, 'sqlstate', None)
            if pgcode == '23503':
                raise OwnershipAssignmentForeignKeyError('Referenced entity not found (concurrent delete)') from exc
            if pgcode == '23505':
                raise OwnershipAssignmentDuplicateError(
                    'Ownership assignment already exists for this subject/target/kind'
                ) from exc
            if pgcode == '23514':
                raise OwnershipAssignmentTargetRequiredError('XOR constraint violated') from exc
            raise

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.ownership_assignment.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'assignment_id': str(assignment.id),
                    'subject_id': str(subject_id),
                    'resource_id': str(resource_id) if resource_id is not None else None,
                    'account_id': str(account_id) if account_id is not None else None,
                    'kind': kind.value,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(assignment.id),
            )
        )
        return assignment

    async def get_assignment(
        self,
        session: AsyncSession,
        assignment_id: uuid.UUID,
    ) -> OwnershipAssignment | None:
        """Get ownership assignment by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_by_id(session, assignment_id)

    async def list_assignments(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        kind: OwnershipKind | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OwnershipAssignment]:
        """List ownership assignments with optional filters. No logging."""
        return await repo_list(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=account_id,
            kind=kind,
            limit=limit,
            offset=offset,
        )

    async def delete_assignment(
        self,
        session: AsyncSession,
        assignment_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> None:
        """Delete ownership assignment by id and emit inventory.ownership_assignment.deleted.

        Raises NotFoundError if missing.
        """
        assignment = await repo_get_by_id(session, assignment_id)
        if assignment is None:
            raise OwnershipAssignmentNotFoundError(assignment_id)

        # Snapshot FKs before delete — ORM attributes may expire after flush; needed for envelope payload below
        snap_subject_id = assignment.subject_id
        snap_resource_id = assignment.resource_id
        snap_account_id = assignment.account_id
        snap_kind = assignment.kind

        await repo_delete(session, assignment)

        # target_id uses method-arg assignment_id — post-delete ORM attribute access on the detached row is unsafe
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.ownership_assignment.deleted',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'assignment_id': str(assignment_id),
                    'subject_id': str(snap_subject_id),
                    'resource_id': str(snap_resource_id) if snap_resource_id is not None else None,
                    'account_id': str(snap_account_id) if snap_account_id is not None else None,
                    'kind': snap_kind.value,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(assignment_id),
            )
        )
