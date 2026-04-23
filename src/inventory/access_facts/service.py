# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NoReturn
import uuid

from sqlalchemy import select
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
    get_revoked_access_fact_by_key as repo_get_revoked_access_fact_by_key,
)
from src.inventory.access_facts.repository import (
    list_access_facts as repo_list_access_facts,
)
from src.inventory.access_facts.repository import (
    reactivate_access_fact as repo_reactivate_access_fact,
)
from src.inventory.access_facts.repository import (
    revoke_access_fact as repo_revoke_access_fact,
)
from src.inventory.access_facts.repository import (
    update_access_fact_fields as repo_update_access_fact_fields,
)
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.access_facts'


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class AccessFactNotFoundError(Exception):
    """Raised when an access fact is not found."""

    def __init__(self, fact_id: uuid.UUID) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact not found: {fact_id}')


class DuplicateActiveAccessFactError(Exception):
    """Raised when an active row with the same partial-unique key already exists.

    Strict — not a silent no-op. The caller must check first or handle the error.
    Maps to HTTP 409 in future write routes.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AccessFactForeignKeyError(Exception):
    """Raised when a referenced entity (subject, resource, account) does not exist."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AccessFactActionSlugUnknownError(Exception):
    """Raised when the provided action_slug is not found in ref_actions.

    Maps to HTTP 400 in future write routes.
    """

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f'Unknown action slug: {slug!r}')


class AccessFactApplicationScopeMismatchError(Exception):
    """Raised when Account.application_id != Resource.application_id.

    Enforcement is in the service layer because PostgreSQL does not support
    cross-table CHECK constraints natively; a trigger or materialized-view
    approach would add operational complexity with no current benefit.
    Maps to HTTP 422 in future write routes.
    """

    def __init__(self, account_id: uuid.UUID, resource_id: uuid.UUID) -> None:
        self.account_id = account_id
        self.resource_id = resource_id
        super().__init__(f'Account {account_id} and resource {resource_id} belong to different applications')


class AccessFactNotRevokedError(Exception):
    """Raised by revoke_fact when the target row is already inactive.

    Strict — silent no-op would hide connector bugs that double-revoke.
    Maps to HTTP 409 in future write routes.
    """

    def __init__(self, fact_id: uuid.UUID) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact {fact_id} is already revoked')


class AccessFactNotActiveError(Exception):
    """Raised by refresh_fact_fields when the target row is revoked.

    A revoked fact must be re-granted via create_fact (reactivation path),
    not updated in place.
    Maps to HTTP 409 in future write routes.
    """

    def __init__(self, fact_id: uuid.UUID) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact {fact_id} is not active; use create_fact to reactivate')


# ---------------------------------------------------------------------------
# Module-level helpers (service-layer discipline per ARCH_CONTEXT Step 11)
# ---------------------------------------------------------------------------


async def _resolve_action_id(session: AsyncSession, slug: str) -> int | None:
    """Return ref_actions.id for the given slug, or None if not found."""
    from src.inventory.actions.models import Action as RefAction

    result = await session.execute(select(RefAction.id).where(RefAction.slug == slug))
    return result.scalar_one_or_none()


async def _find_revoked_by_key(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action_id: int,
) -> AccessFact | None:
    """Look up an existing revoked row by partial-unique key."""
    return await repo_get_revoked_access_fact_by_key(
        session,
        subject_id=subject_id,
        account_id=account_id,
        resource_id=resource_id,
        action_id=action_id,
    )


async def _validate_application_scope(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> None:
    """Raise AccessFactApplicationScopeMismatchError if account and resource are in different apps.

    No-op call site when account_id is None — caller must guard.
    Service-layer enforcement: cross-table CHECK is not possible in PostgreSQL without
    triggers or deferred constraints; service check + tests is sufficient at this stage.
    """
    from src.inventory.accounts.models import Account
    from src.inventory.resources.models import Resource

    account = await session.get(Account, account_id)
    resource = await session.get(Resource, resource_id)
    # Both should exist at this point (FK validation precedes scope check in create_fact)
    if account is not None and resource is not None:
        if account.application_id != resource.application_id:
            raise AccessFactApplicationScopeMismatchError(account_id, resource_id)


def _translate_create_integrity_error(exc: IntegrityError) -> NoReturn:
    """Translate IntegrityError to a domain error based on pgcode.

    pgcode 23505 → DuplicateActiveAccessFactError (partial-unique collision on active row).
    pgcode 23503 → AccessFactForeignKeyError.
    Any other pgcode → re-raise.
    """
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    if pgcode == '23505':
        constraint = getattr(getattr(orig, 'diag', None), 'constraint_name', 'unknown')
        raise DuplicateActiveAccessFactError(
            f'Active access fact already exists for this key (constraint: {constraint})'
        ) from exc
    if pgcode == '23503':
        raise AccessFactForeignKeyError(str(exc)) from exc
    raise exc


def _build_created_event(
    fact: AccessFact,
    action_slug: str,
    correlation_id: str,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_fact.created',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'access_fact_id': str(fact.id),
            'subject_id': str(fact.subject_id),
            'account_id': str(fact.account_id) if fact.account_id else None,
            'resource_id': str(fact.resource_id),
            'action_id': fact.action_id,
            'action_slug': action_slug,
            'effect': fact.effect.value,
            'is_active': fact.is_active,
            'valid_from': str(fact.valid_from),
            'valid_until': str(fact.valid_until) if fact.valid_until else None,
            'observed_at': str(fact.observed_at),
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(fact.id),
    )


def _build_reactivated_event(
    fact: AccessFact,
    action_slug: str,
    previous_revoked_at: datetime | None,
    correlation_id: str,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_fact.reactivated',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'access_fact_id': str(fact.id),
            'subject_id': str(fact.subject_id),
            'account_id': str(fact.account_id) if fact.account_id else None,
            'resource_id': str(fact.resource_id),
            'action_id': fact.action_id,
            'action_slug': action_slug,
            'effect': fact.effect.value,
            'previous_revoked_at': str(previous_revoked_at) if previous_revoked_at else None,
            'observed_at': str(fact.observed_at),
            'valid_from': str(fact.valid_from),
            'valid_until': str(fact.valid_until) if fact.valid_until else None,
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(fact.id),
    )


def _build_updated_event(
    fact: AccessFact,
    changed_fields: list[str],
    previous_values: dict,
    observed_at: datetime,
    correlation_id: str,
) -> EventEnvelope:
    """Build EventEnvelope for inventory.access_fact.updated."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_fact.updated',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'fact_id': str(fact.id),
            'changed_fields': changed_fields,
            'previous_values': previous_values,
            'observed_at': str(observed_at),
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(fact.id),
    )


def _build_revoked_event(
    fact: AccessFact,
    action_slug: str,
    correlation_id: str,
) -> EventEnvelope:
    # Payload per phase_12.md emission table: fact_id, subject_id, resource_id,
    # action_id, action_slug, revoked_at. account_id deliberately not included.
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_fact.revoked',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        payload={
            'fact_id': str(fact.id),
            'subject_id': str(fact.subject_id),
            'resource_id': str(fact.resource_id),
            'action_id': fact.action_id,
            'action_slug': action_slug,
            'revoked_at': str(fact.revoked_at),
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(fact.id),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AccessFactService:
    """Orchestrates access fact creation, retrieval, revocation, and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_fact(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID,
        account_id: uuid.UUID | None = None,
        resource_id: uuid.UUID,
        action_slug: str,
        effect: AccessFactEffect,
        observed_at: datetime,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        correlation_id: str | None = None,
    ) -> AccessFact:
        """Create or reactivate an access fact.

        Flow:
        1. Resolve action_slug → action_id (AccessFactActionSlugUnknownError if unknown).
        2. Validate subject, resource, account FK targets exist.
        3. Scope check: when account_id set, Account.application_id must equal Resource.application_id.
        4. Reactivate-or-create:
           - Existing revoked row for key → reactivate in place, emit .reactivated.
           - No existing row → insert, emit .created.
           - Existing ACTIVE row → DuplicateActiveAccessFactError (strict, no silent no-op).
        """
        cid = correlation_id if correlation_id is not None else uuid.uuid4().hex

        # Step 1: resolve action slug
        action_id = await _resolve_action_id(session, action_slug)
        if action_id is None:
            raise AccessFactActionSlugUnknownError(action_slug)

        # Step 2: validate FK targets
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

            # Step 3: application-scope invariant
            await _validate_application_scope(session, account_id=account_id, resource_id=resource_id)

        # Step 4: reactivate-or-create
        existing_revoked = await _find_revoked_by_key(
            session,
            subject_id=subject_id,
            account_id=account_id,
            resource_id=resource_id,
            action_id=action_id,
        )

        if existing_revoked is not None:
            # Capture previous_revoked_at BEFORE mutation (architect requirement)
            previous_revoked_at = existing_revoked.revoked_at
            await repo_reactivate_access_fact(
                session,
                existing_revoked,
                effect=effect,
                observed_at=observed_at,
                valid_from=valid_from,
                valid_until=valid_until,
            )
            await self._events.emit(_build_reactivated_event(existing_revoked, action_slug, previous_revoked_at, cid))
            return existing_revoked

        # Fresh insert path
        try:
            fact = await repo_create_access_fact(
                session,
                subject_id=subject_id,
                account_id=account_id,
                resource_id=resource_id,
                action_id=action_id,
                effect=effect,
                observed_at=observed_at,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        except IntegrityError as exc:
            _translate_create_integrity_error(exc)

        await self._events.emit(_build_created_event(fact, action_slug, cid))
        return fact

    async def revoke_fact(
        self,
        session: AsyncSession,
        fact_id: uuid.UUID,
        *,
        observed_at: datetime,
        correlation_id: str | None = None,
    ) -> AccessFact:
        """Revoke an access fact (is_active=False, stamp revoked_at).

        Emits inventory.access_fact.revoked.
        Raises AccessFactNotFoundError if not found.
        Raises AccessFactNotRevokedError if already revoked (strict — no silent no-op).
        """
        cid = correlation_id if correlation_id is not None else uuid.uuid4().hex

        fact = await repo_get_access_fact_by_id(session, fact_id, with_action_ref=True)
        if fact is None:
            raise AccessFactNotFoundError(fact_id)
        if not fact.is_active:
            raise AccessFactNotRevokedError(fact_id)

        # action_ref is eager-loaded above — no extra SELECT needed
        action_slug = fact.action_ref.slug

        await repo_revoke_access_fact(session, fact, revoked_at=observed_at)
        await self._events.emit(_build_revoked_event(fact, action_slug, cid))
        return fact

    async def refresh_fact_fields(
        self,
        session: AsyncSession,
        fact_id: uuid.UUID,
        *,
        effect: AccessFactEffect,
        valid_from: datetime | None,
        valid_until: datetime | None,
        observed_at: datetime,
        correlation_id: str | None = None,
    ) -> AccessFact:
        """Update mutable fields of an active fact in place.

        Only updates fields that actually changed.  Emits inventory.access_fact.updated
        with the list of changed fields and their previous values.

        Raises:
            AccessFactNotFoundError: when fact_id does not exist.
            AccessFactNotActiveError: when the fact is revoked.
        """
        cid = correlation_id if correlation_id is not None else uuid.uuid4().hex

        fact = await repo_get_access_fact_by_id(session, fact_id)
        if fact is None:
            raise AccessFactNotFoundError(fact_id)
        if not fact.is_active:
            raise AccessFactNotActiveError(fact_id)

        # Compute drift
        changed_fields: list[str] = []
        previous_values: dict = {}

        if fact.effect != effect:
            changed_fields.append('effect')
            previous_values['effect'] = fact.effect.value
        # Only track valid_from change when a new explicit value is provided
        # (valid_from is NOT NULL in schema; None means "don't touch")
        if valid_from is not None and fact.valid_from != valid_from:
            changed_fields.append('valid_from')
            previous_values['valid_from'] = str(fact.valid_from) if fact.valid_from else None
        if fact.valid_until != valid_until:
            changed_fields.append('valid_until')
            previous_values['valid_until'] = str(fact.valid_until) if fact.valid_until else None

        if not changed_fields:
            # No actual drift — still update observed_at and return
            fact.observed_at = observed_at
            await session.flush()
            return fact

        await repo_update_access_fact_fields(
            session,
            fact,
            effect=effect,
            valid_from=valid_from,
            valid_until=valid_until,
            observed_at=observed_at,
        )
        await self._events.emit(_build_updated_event(fact, changed_fields, previous_values, observed_at, cid))
        return fact

    async def get_fact_by_natural_key(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID,
        account_id: uuid.UUID | None,
        resource_id: uuid.UUID,
        action_slug: str,
    ) -> AccessFact | None:
        """Look up an ACTIVE access fact by natural key.

        Returns None when the fact is absent or revoked (active_only=True).
        Unknown action_slug → None (read path is permissive, symmetric with list_facts).
        """
        action_id = await _resolve_action_id(session, action_slug)
        if action_id is None:
            return None
        return await repo_get_access_fact_by_natural_key(
            session,
            subject_id=subject_id,
            account_id=account_id,
            resource_id=resource_id,
            action_id=action_id,
            active_only=True,
        )

    async def get_fact(
        self,
        session: AsyncSession,
        fact_id: uuid.UUID,
    ) -> AccessFact | None:
        """Get access fact by id. No event emitted.

        Eager-loads action_ref so that routes can expose action_slug without N+1.
        """
        return await repo_get_access_fact_by_id(session, fact_id, with_action_ref=True)

    async def list_facts(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        action_slug: str | None = None,
        effect: AccessFactEffect | None = None,
        is_active: bool | None = None,
        valid_at: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AccessFact]:
        """List access facts with optional filters. No event emitted.

        Unknown action_slug → empty list (read path permissive — symmetric with Step 12 Q7).
        is_active=None → both active and revoked rows.
        """
        action_id: int | None = None
        if action_slug is not None:
            action_id = await _resolve_action_id(session, action_slug)
            if action_id is None:
                return []
        return await repo_list_access_facts(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=account_id,
            action_id=action_id,
            effect=effect,
            is_active=is_active,
            valid_at=valid_at,
            limit=limit,
            offset=offset,
            with_action_ref=True,
        )
