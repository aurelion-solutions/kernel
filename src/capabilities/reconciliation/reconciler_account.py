# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account reconciler: maps AccountDTO into Account using the generic engine."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.engine import reconcile_entities
from src.capabilities.reconciliation.schemas import EntityReconciliationResult
from src.inventory.accounts.models import Account
from src.inventory.accounts.repository import list_by_application
from src.inventory.accounts.schemas import AccountDTO


def _get_key_from_dto(dto: AccountDTO) -> str:
    return dto.identifier


def _get_key_from_model(model: Account) -> str | None:
    return model.meta.get('identifier') if isinstance(model.meta, dict) else None


def _create_from_dto(
    session: AsyncSession,
    application_id: uuid.UUID,
    dto: AccountDTO,
) -> Account:
    account = Account(
        application_id=application_id,
        username=dto.username or dto.identifier,
        display_name=dto.display_name,
        email=dto.email,
        is_active=dto.is_active,
        is_privileged=dto.is_privileged,
        mfa_enabled=dto.mfa_enabled,
        meta={**dto.meta, 'identifier': dto.identifier},
    )
    session.add(account)
    return account


def _update_from_dto(model: Account, dto: AccountDTO) -> bool:
    changed = False
    if model.username != (dto.username or dto.identifier):
        model.username = dto.username or dto.identifier
        changed = True
    if model.display_name != dto.display_name:
        model.display_name = dto.display_name
        changed = True
    if model.email != dto.email:
        model.email = dto.email
        changed = True
    if model.is_active != dto.is_active:
        model.is_active = dto.is_active
        changed = True
    if model.is_privileged != dto.is_privileged:
        model.is_privileged = dto.is_privileged
        changed = True
    if model.mfa_enabled != dto.mfa_enabled:
        model.mfa_enabled = dto.mfa_enabled
        changed = True
    meta = {**model.meta, 'identifier': dto.identifier, **dto.meta}
    if model.meta != meta:
        model.meta = meta
        changed = True
    return changed


async def reconcile_accounts(
    session: AsyncSession,
    application_id: uuid.UUID,
    dtos: list[AccountDTO],
) -> EntityReconciliationResult:
    """Reconcile AccountDTOs into Account models for one application."""
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
