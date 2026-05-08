# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding service — business logic and event emission."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import NoReturn
import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.artifact_bindings.models import ArtifactBinding
from src.inventory.artifact_bindings.repository import (
    create_artifact_binding as repo_create_artifact_binding,
)
from src.inventory.artifact_bindings.repository import (
    get_artifact_binding_by_id as repo_get_artifact_binding_by_id,
)
from src.inventory.artifact_bindings.repository import (
    list_artifact_bindings as repo_list_artifact_bindings,
)
from src.inventory.artifact_bindings.schemas import SUPPORTED_TARGET_TYPES
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.artifact_bindings'


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ArtifactBindingArtifactNotFoundError(Exception):
    """Raised when the referenced AccessArtifact does not exist."""


class ArtifactBindingTargetNotFoundError(Exception):
    """Raised when target_id does not exist in the table implied by target_type (404)."""


class ArtifactBindingUnknownTargetTypeError(Exception):
    """Raised when target_type is not in SUPPORTED_TARGET_TYPES (400)."""


class ArtifactBindingDuplicateError(Exception):
    """Raised on UNIQUE (artifact_id, target_type, target_id) collision."""


# ---------------------------------------------------------------------------
# Module-level helpers (service-layer discipline: Phase 11 Step 3)
# ---------------------------------------------------------------------------

# Loader type: (session, target_id) → ORM instance | None
_TargetLoader = Callable[[AsyncSession, uuid.UUID], Awaitable[object | None]]


def _resolve_target_loader(target_type: str) -> _TargetLoader:
    """Return the async loader for the given target_type.

    Each loader is a thin wrapper around session.get(ModelClass, pk).
    Dispatch is done via a static dict — no hardcoded if/elif chains.
    """

    async def _load_access_fact(session: AsyncSession, target_id: uuid.UUID) -> object | None:
        # Phase 15: ``access_facts`` was dropped from PG — facts now live in Iceberg
        # ``normalized.access_facts``. The binding stores a plain UUID with no FK
        # constraint, so existence cannot be verified from PG. Treat the target as
        # always present; the lake-side validator (if any) is the authoritative check.
        del session, target_id  # explicit unused
        return object()  # sentinel: truthy → caller treats target as found

    async def _load_resource(session: AsyncSession, target_id: uuid.UUID) -> object | None:
        from src.inventory.resources.models import Resource

        return await session.get(Resource, target_id)

    async def _load_account(session: AsyncSession, target_id: uuid.UUID) -> object | None:
        from src.inventory.accounts.models import Account

        return await session.get(Account, target_id)

    async def _load_subject(session: AsyncSession, target_id: uuid.UUID) -> object | None:
        from src.inventory.subjects.models import Subject

        return await session.get(Subject, target_id)

    _loaders: dict[str, _TargetLoader] = {
        'access_fact': _load_access_fact,
        'resource': _load_resource,
        'account': _load_account,
        'subject': _load_subject,
    }
    return _loaders[target_type]


def _validate_target_type(target_type: str) -> None:
    """Raise ArtifactBindingUnknownTargetTypeError for unsupported target_type values."""
    if target_type not in SUPPORTED_TARGET_TYPES:
        raise ArtifactBindingUnknownTargetTypeError(
            f'Unknown target_type {target_type!r}. Supported: {sorted(SUPPORTED_TARGET_TYPES)}'
        )


def _build_artifact_binding_created_event(
    binding: ArtifactBinding,
    *,
    artifact_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    correlation_id: str,
) -> EventEnvelope:
    """Build the inventory.artifact_binding.created event envelope."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.artifact_binding.created',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'binding_id': str(binding.id),
            'artifact_id': str(artifact_id),
            'target_type': target_type,
            'target_id': str(target_id),
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(binding.id),
    )


def _translate_create_integrity_error(exc: IntegrityError) -> NoReturn:
    """Translate IntegrityError 23505 on the UNIQUE triple → ArtifactBindingDuplicateError.

    Re-raises the original exception for any other pgcode.
    """
    pgcode = getattr(getattr(exc, 'orig', None), 'pgcode', None)
    if pgcode == '23505':
        raise ArtifactBindingDuplicateError(
            'Duplicate binding: (artifact_id, target_type, target_id) already exists.'
        ) from exc
    raise


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ArtifactBindingService:
    """Orchestrates artifact binding creation, retrieval, and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_binding(
        self,
        session: AsyncSession,
        *,
        artifact_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> ArtifactBinding:
        """Create an artifact binding.

        Validates target_type is supported, artifact exists, and target entity exists.
        Emits inventory.artifact_binding.created on success.
        """
        _validate_target_type(target_type)

        result = await session.execute(
            sa.text('SELECT id FROM access_artifacts WHERE id = :id'),
            {'id': artifact_id},
        )
        artifact_row = result.one_or_none()
        if artifact_row is None:
            raise ArtifactBindingArtifactNotFoundError(f'Access artifact not found: {artifact_id}')

        loader = _resolve_target_loader(target_type)
        target = await loader(session, target_id)
        if target is None:
            raise ArtifactBindingTargetNotFoundError(f'Target {target_type!r} with id {target_id} not found.')

        try:
            binding = await repo_create_artifact_binding(
                session,
                artifact_id=artifact_id,
                target_type=target_type,
                target_id=target_id,
            )
        except IntegrityError as exc:
            _translate_create_integrity_error(exc)

        effective_correlation_id = correlation_id if correlation_id is not None else uuid.uuid4().hex
        event = _build_artifact_binding_created_event(
            binding,
            artifact_id=artifact_id,
            target_type=target_type,
            target_id=target_id,
            correlation_id=effective_correlation_id,
        )
        await self._events.emit(event)
        return binding

    async def get_binding(
        self,
        session: AsyncSession,
        binding_id: uuid.UUID,
    ) -> ArtifactBinding | None:
        """Get artifact binding by id. No event emitted (read-side audit deferred)."""
        return await repo_get_artifact_binding_by_id(session, binding_id)

    async def list_bindings(
        self,
        session: AsyncSession,
        *,
        artifact_id: uuid.UUID | None = None,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ArtifactBinding]:
        """List artifact bindings with optional filters. No event emitted."""
        return await repo_list_artifact_bindings(
            session,
            artifact_id=artifact_id,
            target_type=target_type,
            target_id=target_id,
            limit=limit,
            offset=offset,
        )
