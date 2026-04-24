# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityMapping repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).

_UNSET sentinel: used to distinguish "not provided" from "provided as None" in PATCH operations.
Import _UNSET from this module in service.py to construct repository calls cleanly.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping

# Sentinel for "field not provided in PATCH payload" — distinct from None
_UNSET: Any = object()


async def insert_capability_mapping(
    session: AsyncSession,
    *,
    capability_id: int,
    application_id: UUID | None,
    resource_id: UUID | None,
    resource_kind: str | None,
    resource_path_glob: str | None,
    action_slug: str | None,
    scope_key_id: int,
    scope_value_source: dict,
    is_active: bool,
    created_by: str | None,
) -> CapabilityMapping:
    """Insert a new CapabilityMapping row and flush. Does not commit.

    scope_value_source must be a dict (already serialized from the discriminated union).
    """
    mapping = CapabilityMapping(
        capability_id=capability_id,
        application_id=application_id,
        resource_id=resource_id,
        resource_kind=resource_kind,
        resource_path_glob=resource_path_glob,
        action_slug=action_slug,
        scope_key_id=scope_key_id,
        scope_value_source=scope_value_source,
        is_active=is_active,
        created_by=created_by,
    )
    session.add(mapping)
    await session.flush()
    await session.refresh(mapping)
    return mapping


async def get_capability_mapping_by_id(
    session: AsyncSession,
    mapping_id: int,
) -> CapabilityMapping | None:
    """Return the CapabilityMapping with the given id, or None."""
    stmt = select(CapabilityMapping).where(CapabilityMapping.id == mapping_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_capability_mappings(
    session: AsyncSession,
    *,
    capability_id: int | None = None,
    application_id: UUID | None = None,
    scope_key_id: int | None = None,
    is_active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CapabilityMapping]:
    """Return capability mappings ordered by id ASC, with optional filters."""
    stmt = select(CapabilityMapping).order_by(CapabilityMapping.id.asc())
    if capability_id is not None:
        stmt = stmt.where(CapabilityMapping.capability_id == capability_id)
    if application_id is not None:
        stmt = stmt.where(CapabilityMapping.application_id == application_id)
    if scope_key_id is not None:
        stmt = stmt.where(CapabilityMapping.scope_key_id == scope_key_id)
    if is_active is not None:
        stmt = stmt.where(CapabilityMapping.is_active == is_active)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_capability_mapping_fields(
    session: AsyncSession,
    mapping: CapabilityMapping,
    *,
    resource_id: Any = _UNSET,
    resource_kind: Any = _UNSET,
    resource_path_glob: Any = _UNSET,
    action_slug: Any = _UNSET,
    scope_key_id: Any = _UNSET,
    scope_value_source: Any = _UNSET,
    is_active: Any = _UNSET,
) -> CapabilityMapping:
    """Update only explicitly provided fields on the mapping, flush, and return refreshed entity.

    capability_id, application_id, and created_by are intentionally absent — they are immutable.
    Parameters set to _UNSET are not modified. Parameters set to None are written as NULL.
    """
    if resource_id is not _UNSET:
        mapping.resource_id = resource_id
    if resource_kind is not _UNSET:
        mapping.resource_kind = resource_kind
    if resource_path_glob is not _UNSET:
        mapping.resource_path_glob = resource_path_glob
    if action_slug is not _UNSET:
        mapping.action_slug = action_slug
    if scope_key_id is not _UNSET:
        mapping.scope_key_id = scope_key_id
    if scope_value_source is not _UNSET:
        mapping.scope_value_source = scope_value_source
    if is_active is not _UNSET:
        mapping.is_active = is_active
    await session.flush()
    await session.refresh(mapping)
    return mapping


async def delete_capability_mapping(
    session: AsyncSession,
    mapping: CapabilityMapping,
) -> None:
    """Delete a CapabilityMapping and flush. Does not commit."""
    await session.delete(mapping)
    await session.flush()
