# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit service — business logic and event emission.

Two-pass parent resolution invariant
-------------------------------------
Phase 16 Step 15 requires bulk-upserting org_units that may reference each
other as parents *in the same batch* (child listed before parent in CSV).

Algorithm:
  Pass 1 — INSERT ... ON CONFLICT (external_id) DO UPDATE SET name=excluded.name
            with parent_id=NULL for all rows.  Collect external_id→id from RETURNING.

  Pass 2 — For items that supplied parent_external_id:
            needed = {item.parent_external_id} − {item.external_id for item in batch}
            (pre-existing parents not in this batch).
            ONE SELECT IN on org_units for needed.
            Merge with pass-1 map.
            ONE batch UPDATE SET parent_id = CASE...END WHERE external_id IN (children).
            Zero per-row SELECT — no N+1.

Known limitation: cyclic parent chains (A→B→A) are NOT validated.  The FK has
no cycle constraint; this is accepted for Step 15 and documented here.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.org_units.models import OrgUnit
from src.inventory.org_units.repository import (
    bulk_upsert_org_units_by_external_id as repo_bulk_upsert,
)
from src.inventory.org_units.repository import (
    get_by_external_ids as repo_get_by_external_ids,
)
from src.inventory.org_units.repository import (
    update_parents_by_external_id as repo_update_parents,
)
from src.inventory.org_units.schemas import OrgUnitBulkItem
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.org_units'


class OrgUnitParentNotFoundError(Exception):
    """Raised when a parent_external_id cannot be resolved to any known org_unit."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f'Unknown parent_external_ids: {", ".join(missing)}')


def _build_bulk_upserted_event(
    org_units: list[OrgUnit],
    correlation_id: str | None,
) -> EventEnvelope:
    """Build the inventory.org_unit.bulk_upserted event envelope."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.org_unit.bulk_upserted',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'count': len(org_units),
            'external_ids': [ou.external_id for ou in org_units],
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id='org_units',
    )


class OrgUnitService:
    """Orchestrates org_unit bulk-upsert with two-pass parent resolution."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def bulk_upsert_org_units(
        self,
        session: AsyncSession,
        items: list[OrgUnitBulkItem],
        correlation_id: str | None = None,
    ) -> list[OrgUnit]:
        """Bulk-upsert org_units and resolve parent references in two passes.

        See module docstring for the two-pass algorithm details.

        Raises:
            OrgUnitParentNotFoundError: if any parent_external_id resolves to nothing.

        """
        # --- Pass 1: upsert all rows with parent_id=NULL -----------------------
        pairs = [(item.external_id, item.name) for item in items]
        org_units = await repo_bulk_upsert(session, pairs)
        await session.flush()

        # Build external_id → id map from pass-1 results.
        batch_map: dict[str, str] = {ou.external_id: str(ou.id) for ou in org_units}

        # --- Pass 2: resolve parent references ---------------------------------
        items_with_parent = [item for item in items if item.parent_external_id is not None]

        if items_with_parent:
            batch_external_ids = {item.external_id for item in items}
            needed_from_db: set[str] = set()
            for item in items_with_parent:
                peid = item.parent_external_id
                assert peid is not None  # guard for type checker
                if peid not in batch_external_ids:
                    needed_from_db.add(peid)

            # One SELECT IN for pre-existing parents not in this batch.
            pre_existing: dict[str, str] = {}
            if needed_from_db:
                found = await repo_get_by_external_ids(session, list(needed_from_db))
                pre_existing = {ext_id: str(uid) for ext_id, uid in found.items()}

            # Merge maps: batch results + pre-existing DB results.
            parent_map: dict[str, str] = {**batch_map, **pre_existing}

            # Check for unresolved parents.
            missing = [
                item.parent_external_id for item in items_with_parent if item.parent_external_id not in parent_map
            ]
            if missing:
                raise OrgUnitParentNotFoundError(missing)

            # One batch UPDATE for all children.
            child_to_parent_id: dict[str, str] = {
                item.external_id: parent_map[item.parent_external_id]  # type: ignore[index]
                for item in items_with_parent
            }
            await repo_update_parents(session, child_to_parent_id)
            await session.flush()

            # Refresh parent_id on returned rows.
            for ou in org_units:
                if ou.external_id in child_to_parent_id:
                    ou.parent_id = uuid.UUID(child_to_parent_id[ou.external_id])

        await self._events.emit(_build_bulk_upserted_event(org_units, correlation_id))
        return org_units
