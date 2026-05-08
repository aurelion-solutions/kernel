# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityScopeKey service — business logic for the CapabilityScopeKey vocabulary slice."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_model.capability_scope_keys.exceptions import (
    CapabilityScopeKeyCodeAlreadyExistsError,
    CapabilityScopeKeyNotFoundError,
)
from src.inventory.access_model.capability_scope_keys.repository import (
    get_capability_scope_key_by_id,
    insert_capability_scope_key,
    list_capability_scope_keys,
    update_capability_scope_key_fields,
)
from src.inventory.access_model.capability_scope_keys.schemas import (
    CapabilityScopeKeyCreate,
    CapabilityScopeKeyPatch,
    CapabilityScopeKeyRead,
)
from src.platform.logs.service import LogService


def _translate_insert_integrity_error(exc: IntegrityError, code: str) -> None:
    """Translate IntegrityError from insert into a domain error, or re-raise.

    asyncpg wraps the original error as exc.orig.__cause__; constraint_name lives there.
    """
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    # constraint_name is on the underlying asyncpg exception, not on the SQLAlchemy wrapper
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint_name: str | None = getattr(asyncpg_exc, 'constraint_name', None)
    if pgcode == '23505' and constraint_name == 'uq_capability_scope_keys_code':
        raise CapabilityScopeKeyCodeAlreadyExistsError(code) from None
    raise exc


class CapabilityScopeKeyService:
    """CRUD service for the CapabilityScopeKey vocabulary.

    ``log_service`` is plumbed for parity with sibling slices but is not used in Step 2.
    No events and no logs are emitted by this service in Phase 13 Step 2 — CapabilityScopeKey
    is vocabulary infrastructure; the Phase 13 event catalog does not include scope-key changes.
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def create(self, payload: CapabilityScopeKeyCreate) -> CapabilityScopeKeyRead:
        """Create a new CapabilityScopeKey. Raises CapabilityScopeKeyCodeAlreadyExistsError on duplicate code."""
        try:
            scope_key = await insert_capability_scope_key(
                self._session,
                code=payload.code,
                name=payload.name,
                description=payload.description,
                is_active=payload.is_active,
                created_by=payload.created_by,
            )
        except IntegrityError as exc:
            _translate_insert_integrity_error(exc, payload.code)
        return CapabilityScopeKeyRead.model_validate(scope_key)

    async def list(
        self,
        *,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CapabilityScopeKeyRead]:
        """Return capability scope keys, optionally filtered by is_active."""
        rows = await list_capability_scope_keys(
            self._session,
            is_active=is_active,
            limit=limit,
            offset=offset,
        )
        return [CapabilityScopeKeyRead.model_validate(row) for row in rows]

    async def get(self, scope_key_id: int) -> CapabilityScopeKeyRead:
        """Return a CapabilityScopeKey by id. Raises CapabilityScopeKeyNotFoundError when missing."""
        scope_key = await get_capability_scope_key_by_id(self._session, scope_key_id)
        if scope_key is None:
            raise CapabilityScopeKeyNotFoundError(scope_key_id)
        return CapabilityScopeKeyRead.model_validate(scope_key)

    async def patch(self, scope_key_id: int, payload: CapabilityScopeKeyPatch) -> CapabilityScopeKeyRead:
        """Update provided fields on a CapabilityScopeKey. Raises CapabilityScopeKeyNotFoundError when missing.

        # code is immutable after creation — never updatable via PATCH or any other path
        """
        scope_key = await get_capability_scope_key_by_id(self._session, scope_key_id)
        if scope_key is None:
            raise CapabilityScopeKeyNotFoundError(scope_key_id)
        scope_key = await update_capability_scope_key_fields(
            self._session,
            scope_key,
            name=payload.name,
            description=payload.description,
            is_active=payload.is_active,
        )
        return CapabilityScopeKeyRead.model_validate(scope_key)

    async def deactivate(self, scope_key_id: int) -> CapabilityScopeKeyRead:
        """Soft-delete a CapabilityScopeKey by setting is_active=False.

        Idempotent: calling twice still returns is_active=False without error.
        Raises CapabilityScopeKeyNotFoundError when the scope key does not exist.
        """
        scope_key = await get_capability_scope_key_by_id(self._session, scope_key_id)
        if scope_key is None:
            raise CapabilityScopeKeyNotFoundError(scope_key_id)
        scope_key = await update_capability_scope_key_fields(
            self._session,
            scope_key,
            is_active=False,
        )
        return CapabilityScopeKeyRead.model_validate(scope_key)
