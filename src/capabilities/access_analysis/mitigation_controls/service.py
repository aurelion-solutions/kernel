# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationControl service — business logic for the MitigationControl catalog slice."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.mitigation_controls.exceptions import (
    MitigationControlCodeAlreadyExistsError,
    MitigationControlNotFoundError,
)
from src.capabilities.access_analysis.mitigation_controls.models import MitigationControlType
from src.capabilities.access_analysis.mitigation_controls.repository import (
    get_mitigation_control_by_id,
    insert_mitigation_control,
    list_mitigation_controls,
    update_mitigation_control_fields,
)
from src.capabilities.access_analysis.mitigation_controls.schemas import (
    MitigationControlCreate,
    MitigationControlPatch,
    MitigationControlRead,
)
from src.platform.logs.service import LogService


def _translate_insert_integrity_error(exc: IntegrityError, code: str) -> None:
    """Translate IntegrityError from insert into a domain error, or re-raise.

    asyncpg wraps the original error as exc.orig.__cause__; constraint_name lives there.
    """
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint_name: str | None = getattr(asyncpg_exc, 'constraint_name', None)
    if pgcode == '23505' and constraint_name == 'uq_mitigation_controls_code':
        raise MitigationControlCodeAlreadyExistsError(code) from None
    raise exc


class MitigationControlService:
    """CRUD service for the MitigationControl reference catalog.

    ``log_service`` is plumbed for parity with other slices but is not used in Step 8.
    No events and no logs are emitted by this service — MitigationControl is reference
    catalog infrastructure; the Phase 13 event catalog does not include
    ``mitigation_control.created`` at this step.
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def create(self, payload: MitigationControlCreate) -> MitigationControlRead:
        """Create a new MitigationControl. Raises MitigationControlCodeAlreadyExistsError on duplicate code."""
        try:
            control = await insert_mitigation_control(
                self._session,
                code=payload.code,
                name=payload.name,
                description=payload.description,
                type=payload.type,
                is_active=payload.is_active,
                created_by=payload.created_by,
            )
        except IntegrityError as exc:
            _translate_insert_integrity_error(exc, payload.code)
        return MitigationControlRead.model_validate(control)

    async def list(
        self,
        *,
        is_active: bool | None = None,
        type: MitigationControlType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MitigationControlRead]:
        """Return mitigation controls, optionally filtered by is_active and/or type."""
        rows = await list_mitigation_controls(
            self._session,
            is_active=is_active,
            type=type,
            limit=limit,
            offset=offset,
        )
        return [MitigationControlRead.model_validate(row) for row in rows]

    async def get(self, control_id: int) -> MitigationControlRead:
        """Return a MitigationControl by id. Raises MitigationControlNotFoundError when missing."""
        control = await get_mitigation_control_by_id(self._session, control_id)
        if control is None:
            raise MitigationControlNotFoundError(control_id)
        return MitigationControlRead.model_validate(control)

    async def patch(self, control_id: int, payload: MitigationControlPatch) -> MitigationControlRead:
        """Update provided fields on a MitigationControl. Raises MitigationControlNotFoundError when missing.

        code is immutable after creation — never updatable via PATCH or any other path.
        """
        control = await get_mitigation_control_by_id(self._session, control_id)
        if control is None:
            raise MitigationControlNotFoundError(control_id)
        control = await update_mitigation_control_fields(
            self._session,
            control,
            name=payload.name,
            description=payload.description,
            type=payload.type,
            is_active=payload.is_active,
        )
        return MitigationControlRead.model_validate(control)

    async def deactivate(self, control_id: int) -> MitigationControlRead:
        """Soft-delete a MitigationControl by setting is_active=False.

        Idempotent: calling twice still returns is_active=False without error.
        Raises MitigationControlNotFoundError when the control does not exist.
        """
        control = await get_mitigation_control_by_id(self._session, control_id)
        if control is None:
            raise MitigationControlNotFoundError(control_id)
        control = await update_mitigation_control_fields(
            self._session,
            control,
            is_active=False,
        )
        return MitigationControlRead.model_validate(control)
