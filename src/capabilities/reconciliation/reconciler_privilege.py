# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Privilege reconciler: maps PrivilegeDTO into Privilege using the generic engine."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.engine import reconcile_entities
from src.capabilities.reconciliation.schemas import EntityReconciliationResult
from src.inventory.privileges.models import Privilege
from src.inventory.privileges.repository import list_by_application
from src.inventory.privileges.schemas import PrivilegeDTO


def _get_key_from_dto(dto: PrivilegeDTO) -> str:
    return dto.identifier


def _get_key_from_model(model: Privilege) -> str | None:
    return model.meta.get('identifier') if isinstance(model.meta, dict) else None


def _create_from_dto(
    session: AsyncSession,
    application_id: uuid.UUID,
    dto: PrivilegeDTO,
) -> Privilege:
    priv = Privilege(
        application_id=application_id,
        name=dto.name or dto.identifier,
        display_name=dto.display_name,
        type=dto.type,
        is_active=dto.is_active,
        meta={**dto.meta, 'identifier': dto.identifier},
    )
    session.add(priv)
    return priv


def _update_from_dto(model: Privilege, dto: PrivilegeDTO) -> bool:
    changed = False
    if model.name != (dto.name or dto.identifier):
        model.name = dto.name or dto.identifier
        changed = True
    if model.display_name != dto.display_name:
        model.display_name = dto.display_name
        changed = True
    if model.type != dto.type:
        model.type = dto.type
        changed = True
    if model.is_active != dto.is_active:
        model.is_active = dto.is_active
        changed = True
    meta = {**model.meta, 'identifier': dto.identifier, **dto.meta}
    if model.meta != meta:
        model.meta = meta
        changed = True
    return changed


async def reconcile_privileges(
    session: AsyncSession,
    application_id: uuid.UUID,
    dtos: list[PrivilegeDTO],
) -> EntityReconciliationResult:
    """Reconcile PrivilegeDTOs into Privilege models for one application."""
    return await reconcile_entities(
        session,
        application_id,
        dtos,
        load_existing=lambda s, aid: list_by_application(s, aid),
        get_key_from_dto=_get_key_from_dto,
        get_key_from_model=_get_key_from_model,
        create_from_dto=_create_from_dto,
        update_from_dto=_update_from_dto,
    )
