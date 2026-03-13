# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Generic reconciliation engine for upsert-style synchronization from DTOs to ORM models."""

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.schemas import EntityReconciliationResult

TModel = TypeVar('TModel')
TDTO = TypeVar('TDTO')


async def reconcile_entities(
    session: AsyncSession,
    application_id: Any,
    dtos: list[TDTO],
    *,
    load_existing: Callable[[AsyncSession, Any], Awaitable[list[TModel]]],
    get_key_from_dto: Callable[[TDTO], str],
    get_key_from_model: Callable[[TModel], str | None],
    create_from_dto: Callable[[AsyncSession, Any, TDTO], TModel],
    update_from_dto: Callable[[TModel, TDTO], bool],
) -> EntityReconciliationResult:
    """
    Reconcile incoming DTOs with existing local models.

    - Creates new records when key does not exist
    - Updates existing records when mapped fields changed
    - Marks missing local records inactive when absent from source
    - Returns reconciliation counters
    """
    result = EntityReconciliationResult(source_total=len(dtos))

    existing = await load_existing(session, application_id)
    lookup: dict[str, TModel] = {}
    for model in existing:
        key = get_key_from_model(model)
        if key is not None:
            lookup[key] = model

    source_keys: set[str] = set()

    for dto in dtos:
        key = get_key_from_dto(dto)
        source_keys.add(key)

        if key in lookup:
            model = lookup[key]
            if update_from_dto(model, dto):
                result.updated += 1
            else:
                result.unchanged += 1
            del lookup[key]
        else:
            try:
                create_from_dto(session, application_id, dto)
                result.created += 1
            except Exception:
                result.errors += 1

    for model in lookup.values():
        if hasattr(model, 'is_active'):
            model.is_active = False
            result.deactivated += 1
        else:
            result.errors += 1

    return result
