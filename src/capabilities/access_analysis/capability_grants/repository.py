# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Thin SQL layer for the CapabilityGrant slice — no business logic, no events.

Invariants:
- This module MUST NOT import LogService or emit events.
- upsert_capability_grants raises on DB errors — it never swallows them.
- Conflict resolution targets the named constraint uq_capability_grants_source_pair.
- application_id is INTENTIONALLY OMITTED from ON CONFLICT DO UPDATE set_.
  Re-projection must never overwrite application_id — it is immutable post-projection.

Note on insert/update split:
  capability_grants is NOT partitioned (unlike effective_grants), so xmax in RETURNING
  would technically work. However, we use the same pre-SELECT pattern for consistency
  with EAS and to keep the option of future partitioning without refactoring the writer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capability_grants.capability_projector import CapabilityGrantDraft
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant


@dataclass(frozen=True)
class UpsertResult:
    """Counts returned by upsert_capability_grants."""

    rows_upserted: int
    rows_inserted: int
    rows_updated: int
    rows_tombstoned: int


async def upsert_capability_grants(
    session: AsyncSession,
    drafts: Sequence[CapabilityGrantDraft],
) -> UpsertResult:
    """Bulk-upsert CapabilityGrantDraft rows via ON CONFLICT on uq_capability_grants_source_pair.

    Pre-selects existing source pairs to determine inserts vs updates (mirror EAS pattern).
    application_id is INTENTIONALLY OMITTED from set_= — it is immutable post-projection.
    Never swallows DB errors.
    """
    if not drafts:
        return UpsertResult(rows_upserted=0, rows_inserted=0, rows_updated=0, rows_tombstoned=0)

    # Step 1: collect source pairs that already exist.
    source_pairs = [(draft.source_effective_grant_id, draft.source_capability_mapping_id) for draft in drafts]
    existing_result = await session.execute(
        sa.select(
            CapabilityGrant.source_effective_grant_id,
            CapabilityGrant.source_capability_mapping_id,
        ).where(
            sa.tuple_(
                CapabilityGrant.source_effective_grant_id,
                CapabilityGrant.source_capability_mapping_id,
            ).in_(source_pairs)
        )
    )
    existing_pairs: set[tuple[UUID, int]] = {
        (row.source_effective_grant_id, row.source_capability_mapping_id) for row in existing_result.all()
    }

    # Step 2: upsert.
    values = [
        {
            'subject_id': draft.subject_id,
            'capability_id': draft.capability_id,
            'scope_key_id': draft.scope_key_id,
            'scope_value': draft.scope_value,
            # application_id set on insert from draft; NOT in set_= below (immutable).
            'application_id': draft.application_id,
            'source_effective_grant_id': draft.source_effective_grant_id,
            'source_capability_mapping_id': draft.source_capability_mapping_id,
            'observed_at': draft.observed_at,
            'tombstoned_at': draft.tombstoned_at,
        }
        for draft in drafts
    ]

    insert_stmt = pg_insert(CapabilityGrant).values(values)
    excluded = insert_stmt.excluded
    final_stmt = insert_stmt.on_conflict_do_update(
        constraint='uq_capability_grants_source_pair',
        set_=dict(
            subject_id=excluded.subject_id,
            capability_id=excluded.capability_id,
            scope_key_id=excluded.scope_key_id,
            scope_value=excluded.scope_value,
            # application_id INTENTIONALLY OMITTED — immutable post-projection.
            # On insert it's set from excluded.application_id via the initial values=;
            # on conflict it's preserved unchanged.
            observed_at=excluded.observed_at,
            tombstoned_at=excluded.tombstoned_at,
        ),
    ).returning(
        CapabilityGrant.id,
        CapabilityGrant.tombstoned_at,
        CapabilityGrant.source_effective_grant_id,
        CapabilityGrant.source_capability_mapping_id,
    )

    result = await session.execute(final_stmt)
    rows = result.all()

    rows_upserted = len(rows)
    rows_inserted = sum(
        1 for r in rows if (r.source_effective_grant_id, r.source_capability_mapping_id) not in existing_pairs
    )
    rows_updated = rows_upserted - rows_inserted
    rows_tombstoned = sum(1 for r in rows if r.tombstoned_at is not None)

    return UpsertResult(
        rows_upserted=rows_upserted,
        rows_inserted=rows_inserted,
        rows_updated=rows_updated,
        rows_tombstoned=rows_tombstoned,
    )


async def tombstone_capability_grants_for_effective_grant(
    session: AsyncSession,
    *,
    effective_grant_id: UUID,
    observed_at: datetime,
) -> int:
    """Idempotent soft-delete of every capability grant sourced from this effective grant.

    UPDATE capability_grants SET tombstoned_at=:observed_at, observed_at=:observed_at
    WHERE source_effective_grant_id=:eg_id
    AND (tombstoned_at IS NULL OR tombstoned_at > :observed_at)
    AND observed_at < :observed_at

    Returns rowcount (0 = no-op). Application-side cascade replacing the DB-level FK
    that cannot be created on a partitioned table (see model docstring).
    """
    stmt = (
        sa.update(CapabilityGrant)
        .where(
            CapabilityGrant.source_effective_grant_id == effective_grant_id,
            sa.or_(
                CapabilityGrant.tombstoned_at.is_(None),
                CapabilityGrant.tombstoned_at > observed_at,
            ),
            CapabilityGrant.observed_at < observed_at,
        )
        .values(tombstoned_at=observed_at, observed_at=observed_at)
    )
    result = await session.execute(stmt)
    return result.rowcount  # type: ignore[attr-defined,no-any-return]


async def list_capability_grants(
    session: AsyncSession,
    *,
    subject_id: UUID | None = None,
    capability_id: int | None = None,
    scope_key_id: int | None = None,
    scope_value: str | None = None,
    application_id: UUID | None = None,
    source_effective_grant_id: UUID | None = None,
    source_capability_mapping_id: int | None = None,
    active_only: bool = True,
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
) -> list[CapabilityGrant]:
    """Return capability grants matching the given filters.

    active_only=True (default) appends tombstoned_at IS NULL OR tombstoned_at > :now.
    Results are ordered observed_at DESC, id DESC for stable pagination.
    """
    from datetime import UTC

    _now = now if now is not None else datetime.now(UTC)
    conditions: list[sa.ColumnElement] = []  # type: ignore[type-arg]

    if subject_id is not None:
        conditions.append(CapabilityGrant.subject_id == subject_id)
    if capability_id is not None:
        conditions.append(CapabilityGrant.capability_id == capability_id)
    if scope_key_id is not None:
        conditions.append(CapabilityGrant.scope_key_id == scope_key_id)
    if scope_value is not None:
        conditions.append(CapabilityGrant.scope_value == scope_value)
    if application_id is not None:
        conditions.append(CapabilityGrant.application_id == application_id)
    if source_effective_grant_id is not None:
        conditions.append(CapabilityGrant.source_effective_grant_id == source_effective_grant_id)
    if source_capability_mapping_id is not None:
        conditions.append(CapabilityGrant.source_capability_mapping_id == source_capability_mapping_id)
    if active_only:
        conditions.append(
            sa.or_(
                CapabilityGrant.tombstoned_at.is_(None),
                CapabilityGrant.tombstoned_at > _now,
            )
        )

    stmt = (
        sa.select(CapabilityGrant)
        .where(*conditions)
        .order_by(CapabilityGrant.observed_at.desc(), CapabilityGrant.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_capability_grant(
    session: AsyncSession,
    grant_id: int,
) -> CapabilityGrant | None:
    """Fetch a single CapabilityGrant row by id."""
    stmt = sa.select(CapabilityGrant).where(CapabilityGrant.id == grant_id)
    result = await session.execute(stmt)
    return result.scalars().one_or_none()


async def count_grants_for_mapping(
    session: AsyncSession,
    mapping_id: int,
) -> int:
    """Count non-tombstoned CapabilityGrant rows that reference the given mapping.

    Used by capability_mappings.service._count_dependent_capability_grants.
    Counting only non-tombstoned rows — a tombstoned grant is not "in use".
    """
    stmt = sa.select(sa.func.count()).where(
        CapabilityGrant.source_capability_mapping_id == mapping_id,
        CapabilityGrant.tombstoned_at.is_(None),
    )
    result = await session.execute(stmt)
    return result.scalar_one()
