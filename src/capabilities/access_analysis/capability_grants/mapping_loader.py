# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Neutral loader for active CapabilityMappings.

Extracted from capability_grants/service.py so that the read-only
CapabilityResolverService can import a loader without taking a dependency
on the projection writer service.

Forbidden in this module: flush, commit, rollback, emit_safe, emit_log, emit,
any INSERT/UPDATE/DELETE statement.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capability_grants.capability_projector import CapabilityMappingView
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping


async def load_active_mappings(session: AsyncSession) -> list[CapabilityMappingView]:
    """Load all active CapabilityMappings and return as view objects.

    Cardinality concern: in production this set may grow to thousands.
    For Phase 13 this is acceptable — the optimization (pre-filter by
    application_id / resource_id / resource_kind) belongs to the ScanEngine
    in Step 14.
    """
    stmt = sa.select(CapabilityMapping).where(CapabilityMapping.is_active.is_(True))
    result = await session.execute(stmt)
    mappings = result.scalars().all()

    return [
        CapabilityMappingView(
            id=m.id,
            capability_id=m.capability_id,
            application_id=m.application_id,
            resource_id=m.resource_id,
            resource_kind=m.resource_kind,
            resource_path_glob=m.resource_path_glob,
            action_slug=m.action_slug,
            scope_key_id=m.scope_key_id,
            scope_value_source=m.scope_value_source,
            is_active=m.is_active,
        )
        for m in mappings
    ]
