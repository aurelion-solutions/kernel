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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.org_units.models import OrgUnit
from src.inventory.org_units.repository import (
    bulk_upsert_org_units_by_external_id as repo_bulk_upsert,
)
from src.inventory.org_units.repository import (
    create_org_unit as repo_create_org_unit,
)
from src.inventory.org_units.repository import (
    delete_org_unit as repo_delete_org_unit,
)
from src.inventory.org_units.repository import (
    get_by_external_ids as repo_get_by_external_ids,
)
from src.inventory.org_units.repository import (
    get_org_unit as repo_get_org_unit,
)
from src.inventory.org_units.repository import (
    update_org_unit as repo_update_org_unit,
)
from src.inventory.org_units.repository import (
    update_parents_by_external_id as repo_update_parents,
)
from src.inventory.org_units.schemas import OrgUnitBulkItem, OrgUnitCreate, OrgUnitUpdate
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.org_units'


class OrgUnitParentNotFoundError(Exception):
    """Raised when a parent_external_id cannot be resolved to any known org_unit."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f'Unknown parent_external_ids: {", ".join(missing)}')


class OrgUnitNotFoundError(Exception):
    """Raised when an org_unit row cannot be found by primary key."""


class InternalOrgUnitImmutableError(Exception):
    """Raised when a mutation is attempted on an internal org-unit.

    Internal org-units are reconcile-owned — PUT and DELETE are rejected.
    """


class ParentMustBeExternalError(Exception):
    """Raised when parent_id references a non-existent or internal org-unit."""


class DuplicateExternalIdError(Exception):
    """Raised when external_id conflicts with an existing org-unit row."""


def _validate_not_internal(data_is_internal: bool) -> None:
    """Confirm the supplied is_internal value is False (defence-in-depth after Pydantic)."""
    if data_is_internal:
        raise ValueError('is_internal must be False for externally managed org-units')


async def _validate_parent(
    session: AsyncSession,
    parent_id: uuid.UUID,
) -> None:
    """Load the parent row and reject if missing or internal."""
    parent = await repo_get_org_unit(session, parent_id)
    if parent is None or parent.is_internal:
        raise ParentMustBeExternalError(f'parent_id {parent_id} does not reference an existing external org-unit')


def _translate_create_integrity_error(exc: IntegrityError) -> None:
    """Translate IntegrityError on create to DuplicateExternalIdError."""
    raise DuplicateExternalIdError('An org-unit with this external_id already exists') from exc


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
        # NOTE (K-N reachability): this method is used by internal callers /
        # tests only.  The public HTTP POST /org-units/bulk calls
        # OrgUnitLakeService directly and bypasses this PG service entirely.
        # Consequently, is_internal arriving via HTTP /bulk is silently
        # discarded for now (the Iceberg schema does not carry it).  The only
        # way to land an is_internal=False row in PG today is a direct service
        # call (tests / seed scripts).  This is intentional for K-N; producer
        # paths that wire the HTTP route through PG land in a later phase.
        triples = [(item.external_id, item.name, item.is_internal) for item in items]
        org_units = await repo_bulk_upsert(session, triples)
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

    async def create_external_org_unit(
        self,
        session: AsyncSession,
        data: OrgUnitCreate,
    ) -> OrgUnit:
        """Create a single external org-unit.

        Validates:
        - ``data.is_internal`` is False (defence-in-depth after Pydantic).
        - If ``data.parent_id`` is set, the referenced parent must exist and be external.

        Raises:
            ValueError: if ``is_internal`` is True (Pydantic already prevents this
                at the HTTP boundary; this is a service-layer guard).
            ParentMustBeExternalError: if parent_id does not reference an existing
                external org-unit.
            DuplicateExternalIdError: if external_id conflicts with an existing row.
        """
        _validate_not_internal(data.is_internal)
        if data.parent_id is not None:
            await _validate_parent(session, data.parent_id)
        try:
            return await repo_create_org_unit(
                session,
                external_id=data.external_id,
                name=data.name,
                description=data.description,
                is_internal=False,
                parent_id=data.parent_id,
            )
        except IntegrityError as exc:
            _translate_create_integrity_error(exc)
            raise  # unreachable; satisfies type checker

    async def read_org_unit(
        self,
        session: AsyncSession,
        org_unit_id: uuid.UUID,
    ) -> OrgUnit:
        """Return org_unit by id or raise OrgUnitNotFoundError."""
        row = await repo_get_org_unit(session, org_unit_id)
        if row is None:
            raise OrgUnitNotFoundError(f'Org-unit {org_unit_id} not found')
        return row

    async def update_external_org_unit(
        self,
        session: AsyncSession,
        org_unit_id: uuid.UUID,
        patch: OrgUnitUpdate,
    ) -> OrgUnit:
        """Apply a partial update to an external org-unit.

        Only fields supplied by the client (present in ``model_fields_set``) are
        written. ``description=null`` explicitly clears the column.

        Raises:
            OrgUnitNotFoundError: if the row does not exist.
            InternalOrgUnitImmutableError: if the row is internal (reconcile-owned).
        """
        row = await repo_get_org_unit(session, org_unit_id)
        if row is None:
            raise OrgUnitNotFoundError(f'Org-unit {org_unit_id} not found')
        if row.is_internal:
            raise InternalOrgUnitImmutableError('Internal org-units are reconcile-owned')
        fields = patch.model_dump(exclude_unset=True)
        if not fields:
            return row
        updated = await repo_update_org_unit(session, org_unit_id, fields=fields)
        if updated is None:
            raise OrgUnitNotFoundError(f'Org-unit {org_unit_id} not found')
        return updated

    async def delete_external_org_unit(
        self,
        session: AsyncSession,
        org_unit_id: uuid.UUID,
    ) -> None:
        """Delete an external org-unit.

        Employees bound to this org-unit have their org_unit_id set to NULL
        automatically via the FK ``ON DELETE SET NULL``.

        Raises:
            OrgUnitNotFoundError: if the row does not exist.
            InternalOrgUnitImmutableError: if the row is internal (reconcile-owned).
        """
        row = await repo_get_org_unit(session, org_unit_id)
        if row is None:
            raise OrgUnitNotFoundError(f'Org-unit {org_unit_id} not found')
        if row.is_internal:
            raise InternalOrgUnitImmutableError('Internal org-units are reconcile-owned')
        await repo_delete_org_unit(session, org_unit_id)
