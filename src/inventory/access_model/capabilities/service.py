# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Capability service — business logic for the Capability vocabulary slice."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_model.capabilities.exceptions import (
    CapabilityNotFoundError,
    CapabilitySlugAlreadyExistsError,
)
from src.inventory.access_model.capabilities.repository import (
    get_capability_by_id,
    insert_capability,
    list_capabilities,
    update_capability_fields,
)
from src.inventory.access_model.capabilities.schemas import (
    CapabilityCreate,
    CapabilityPatch,
    CapabilityRead,
)
from src.platform.logs.service import LogService


def _translate_insert_integrity_error(exc: IntegrityError, slug: str) -> None:
    """Translate IntegrityError from insert into a domain error, or re-raise.

    asyncpg wraps the original error as exc.orig.__cause__; constraint_name lives there.
    """
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    # constraint_name is on the underlying asyncpg exception, not on the SQLAlchemy wrapper
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint_name: str | None = getattr(asyncpg_exc, 'constraint_name', None)
    if pgcode == '23505' and constraint_name == 'uq_capabilities_slug':
        raise CapabilitySlugAlreadyExistsError(slug) from None
    raise exc


class CapabilityService:
    """CRUD service for the Capability vocabulary.

    ``log_service`` is plumbed for parity with other slices but is not used in Step 1.
    No events and no logs are emitted by this service in Phase 13 Step 1 — Capability is
    vocabulary infrastructure; the Phase 13 event catalog does not include ``capability.created``.
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def create(self, payload: CapabilityCreate) -> CapabilityRead:
        """Create a new Capability. Raises CapabilitySlugAlreadyExistsError on duplicate slug."""
        try:
            capability = await insert_capability(
                self._session,
                slug=payload.slug,
                name=payload.name,
                description=payload.description,
                is_active=payload.is_active,
                created_by=payload.created_by,
            )
        except IntegrityError as exc:
            _translate_insert_integrity_error(exc, payload.slug)
        return CapabilityRead.model_validate(capability)

    async def list(
        self,
        *,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CapabilityRead]:
        """Return capabilities, optionally filtered by is_active."""
        rows = await list_capabilities(
            self._session,
            is_active=is_active,
            limit=limit,
            offset=offset,
        )
        return [CapabilityRead.model_validate(row) for row in rows]

    async def get(self, capability_id: int) -> CapabilityRead:
        """Return a Capability by id. Raises CapabilityNotFoundError when missing."""
        capability = await get_capability_by_id(self._session, capability_id)
        if capability is None:
            raise CapabilityNotFoundError(capability_id)
        return CapabilityRead.model_validate(capability)

    async def patch(self, capability_id: int, payload: CapabilityPatch) -> CapabilityRead:
        """Update provided fields on a Capability. Raises CapabilityNotFoundError when missing.

        # slug is immutable after creation — never updatable via PATCH or any other path
        """
        capability = await get_capability_by_id(self._session, capability_id)
        if capability is None:
            raise CapabilityNotFoundError(capability_id)
        capability = await update_capability_fields(
            self._session,
            capability,
            name=payload.name,
            description=payload.description,
            is_active=payload.is_active,
        )
        return CapabilityRead.model_validate(capability)

    async def deactivate(self, capability_id: int) -> CapabilityRead:
        """Soft-delete a Capability by setting is_active=False.

        Idempotent: calling twice still returns is_active=False without error.
        Raises CapabilityNotFoundError when the capability does not exist.
        """
        capability = await get_capability_by_id(self._session, capability_id)
        if capability is None:
            raise CapabilityNotFoundError(capability_id)
        capability = await update_capability_fields(
            self._session,
            capability,
            is_active=False,
        )
        return CapabilityRead.model_validate(capability)
