# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Role reconciler: maps RoleDTO into Role using the generic engine."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.engine import reconcile_entities
from src.capabilities.reconciliation.schemas import EntityReconciliationResult
from src.inventory.roles.models import Role
from src.inventory.roles.repository import list_by_application
from src.inventory.roles.schemas import RoleDTO


def _get_key_from_dto(dto: RoleDTO) -> str:
    return dto.identifier


def _get_key_from_model(model: Role) -> str | None:
    return model.meta.get('identifier') if isinstance(model.meta, dict) else None


def _create_from_dto(
    session: AsyncSession,
    application_id: uuid.UUID,
    dto: RoleDTO,
) -> Role:
    role = Role(
        application_id=application_id,
        name=dto.name or dto.identifier,
        display_name=dto.display_name,
        type=dto.type,
        is_active=dto.is_active,
        meta={**dto.meta, 'identifier': dto.identifier},
    )
    session.add(role)
    return role


def _update_from_dto(model: Role, dto: RoleDTO) -> bool:
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


async def reconcile_roles(
    session: AsyncSession,
    application_id: uuid.UUID,
    dtos: list[RoleDTO],
) -> EntityReconciliationResult:
    """Reconcile RoleDTOs into Role models for one application."""
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
