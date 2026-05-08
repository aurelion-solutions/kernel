# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure pre-flight capability resolver.

CapabilityResolverService answers "which capability slugs would these sources grant?"
without persisting anything.

Algorithm:
1. Empty sources → return [].
2. Load all active CapabilityMappings via mapping_loader.
3. For each source build a synthetic EffectiveGrantView (placeholder fields for
   fields the matcher does not read).
4. Call capability_projector.matcher_applies for each (source, mapping) pair.
   Collect matched capability_ids.
5. Map capability_ids → slugs via SELECT on capabilities WHERE is_active = true.
   Inactive capabilities are silently dropped.
6. Return sorted(set(slugs)) — alphabetical, distinct.

Forbidden in this module: flush, commit, rollback, emit_safe, emit_log, emit,
any INSERT/UPDATE/DELETE statement, LogService, print, logging.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_analysis.services.schemas import EffectiveGrantRef
from src.inventory.access_model.capability_grants.capability_projector import (
    EffectiveGrantView,
    matcher_applies,
)
from src.inventory.access_model.capability_grants.mapping_loader import load_active_mappings


class CapabilityResolverService:
    """Read-only resolver: sources → distinct capability slugs.

    No flush, no commit, no events. Reads CapabilityMapping + Capability tables
    only. Reuses capability_projector.matcher_applies — single source of truth
    for the matching contract.

    Output: sorted(set(slugs)) — alphabetically sorted, duplicates collapsed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_capabilities_for_sources(
        self,
        *,
        sources: Sequence[EffectiveGrantRef],
    ) -> list[str]:
        """Return the distinct sorted list of capability slugs implied by sources.

        Never touches effective_grants. Never writes to capability_grants or any
        other table. Safe to call for hypothetical (in-memory) sources.

        Args:
            sources: Caller-built EffectiveGrantRef sequence. May be empty.

        Returns:
            Sorted, distinct list of active capability slugs. Returns [] if
            no sources are provided or no mappings match.
        """
        if not sources:
            return []

        active_mappings = await load_active_mappings(self._session)
        if not active_mappings:
            return []

        matched_capability_ids: set[int] = set()

        for source in sources:
            # Build a synthetic EffectiveGrantView from the caller-supplied ref.
            # Placeholder values for fields the three-stage matcher does not read:
            # - id: never read by matcher_applies
            # - subject_id: never read by matcher_applies
            # - tombstoned_at: resolver treats every source as live
            # - resource_attributes / subject_attributes: scope_value resolution
            #   is skipped entirely (slug-only output)
            view = EffectiveGrantView(
                id=uuid4(),
                subject_id=uuid4(),
                application_id=source.application_id,
                resource_id=source.resource_id,
                action_slug=source.action_slug,
                tombstoned_at=None,
                resource_kind=source.resource_kind,
                resource_external_id=source.resource_external_id,
                resource_attributes={},
                subject_attributes={},
            )

            for mapping in active_mappings:
                if matcher_applies(view, mapping):
                    matched_capability_ids.add(mapping.capability_id)

        if not matched_capability_ids:
            return []

        slugs = await self._resolve_slugs(matched_capability_ids)
        return sorted(set(slugs))

    async def _resolve_slugs(self, capability_ids: set[int]) -> list[str]:
        """Fetch slugs for active capabilities by id set."""
        from src.inventory.access_model.capabilities.models import Capability

        stmt = sa.select(Capability.slug).where(
            Capability.id.in_(capability_ids),
            Capability.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
