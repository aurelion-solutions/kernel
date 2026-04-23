# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Thin SQL layer for the Effective Access Store — no business logic, no events.

Responsibilities:
- Fetch ``(AccessFactRow, list[InitiativeRow])`` tuples with all JOIN-resolved
  denormalized fields (``subject_kind`` from ``subjects``,
  ``application_id`` from ``resources``).
- Stream facts per application via a keyset-paginated async iterator so
  ``project_application`` never loads the full fact set into memory.
  Keyset pagination (``WHERE id > :last_id ORDER BY id LIMIT N``) is used
  instead of OFFSET because it is stable under concurrent writes and
  index-friendly for large fact sets.
- Upsert ``EffectiveGrantDraft`` rows via ``INSERT ... ON CONFLICT ON
  CONSTRAINT uq_effective_grants_source_pair DO UPDATE SET (...) RETURNING
  id, tombstoned_at``.

Note on xmax: PostgreSQL 17 does not allow system columns in ``RETURNING``
clauses for partitioned tables (``cannot retrieve a system column in this
context``).  To distinguish inserts from updates we use a two-step approach:
collect pre-existing source-pair keys before the upsert, then compare the
returned ids against that set.  One extra SELECT per batch — acceptable cost.

Invariants:
- This module MUST NOT import ``LogService`` or emit events.  All event
  emission is the sole responsibility of ``service.py``.
- ``upsert_effective_grants`` raises on DB errors — it never swallows them.
- Conflict resolution targets the *named* constraint
  ``uq_effective_grants_source_pair`` (not a column list) so that a future
  constraint rename breaks loudly at test time, not silently at runtime.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import uuid
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.effective_access.models import EffectiveGrant, EffectiveGrantEffect
from src.capabilities.effective_access.projector import EffectiveGrantDraft
from src.capabilities.effective_access.schemas import AccessFactRow, InitiativeRow
from src.inventory.access_facts.models import AccessFact
from src.inventory.actions.models import Action as RefAction
from src.inventory.enums import Action
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind

_BATCH_SIZE = 500  # module-private; configurable via the ``batch_size`` parameter


# ---------------------------------------------------------------------------
# Internal result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UpsertResult:
    """Counts returned by ``upsert_effective_grants``."""

    rows_upserted: int
    rows_inserted: int
    rows_updated: int
    rows_tombstoned: int
    rows_skipped: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fact_row_from_row(row: sa.engine.Row) -> AccessFactRow:  # type: ignore[type-arg]
    # row.action_slug is the slug resolved via JOIN on ref_actions.
    # Map slug → Python Action enum. KeyError on unknown slug = projector contract violation;
    # the slug must round-trip through the seven-slug vocabulary.
    return AccessFactRow(
        id=row.id,
        subject_id=row.subject_id,
        subject_kind=row.subject_kind,
        account_id=row.account_id,
        application_id=row.application_id,
        resource_id=row.resource_id,
        action=Action(row.action_slug),
        effect=row.effect,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
    )


def _initiative_row_from_orm(orm: Initiative) -> InitiativeRow:
    return InitiativeRow(
        id=orm.id,
        access_fact_id=orm.access_fact_id,
        type=orm.type,
        origin=orm.origin,
        valid_from=orm.valid_from,  # type: ignore[arg-type]
        valid_until=orm.valid_until,  # type: ignore[arg-type]
    )


def _build_fact_select() -> sa.Select:  # type: ignore[type-arg]
    """Base SELECT joining access_facts → subjects → resources → ref_actions for denormalization.

    ref_actions JOIN resolves action_id → slug so that the projector boundary
    continues to use the Python Action enum (EAS writes effective_grants.action
    as Enum(Action, name='action', create_type=False) — unchanged in Step 13).
    """
    return (
        sa.select(
            AccessFact.id,
            AccessFact.subject_id,
            Subject.kind.label('subject_kind'),
            AccessFact.account_id,
            Resource.application_id.label('application_id'),
            AccessFact.resource_id,
            RefAction.slug.label('action_slug'),
            AccessFact.effect,
            AccessFact.valid_from,
            AccessFact.valid_until,
        )
        .join(Subject, Subject.id == AccessFact.subject_id)
        .join(Resource, Resource.id == AccessFact.resource_id)
        .join(RefAction, RefAction.id == AccessFact.action_id)
    )


async def _fetch_initiatives_for_facts(
    session: AsyncSession,
    fact_ids: Sequence[UUID],
) -> dict[UUID, list[InitiativeRow]]:
    """Fetch all initiatives for the given fact ids, grouped by access_fact_id."""
    if not fact_ids:
        return {}
    result = await session.execute(sa.select(Initiative).where(Initiative.access_fact_id.in_(fact_ids)))
    groups: dict[UUID, list[InitiativeRow]] = {}
    for init_orm in result.scalars().all():
        row = _initiative_row_from_orm(init_orm)
        groups.setdefault(row.access_fact_id, []).append(row)
    return groups


# ---------------------------------------------------------------------------
# Public query helpers
# ---------------------------------------------------------------------------


async def fetch_access_fact_with_initiatives(
    session: AsyncSession,
    access_fact_id: UUID,
) -> tuple[AccessFactRow, list[InitiativeRow]] | None:
    """Fetch one fact with its initiatives, pre-resolving subject_kind and application_id.

    Returns ``None`` if no fact with the given id exists.
    """
    stmt = _build_fact_select().where(AccessFact.id == access_fact_id)
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None

    fact_row = _fact_row_from_row(row)
    groups = await _fetch_initiatives_for_facts(session, [access_fact_id])
    initiatives = groups.get(access_fact_id, [])
    return fact_row, initiatives


async def fetch_application_facts_with_initiatives(
    session: AsyncSession,
    application_id: UUID,
    *,
    batch_size: int = _BATCH_SIZE,
) -> AsyncIterator[tuple[AccessFactRow, list[InitiativeRow]]]:
    """Keyset-paginated async iterator over all facts for the given application.

    Yields one ``(AccessFactRow, list[InitiativeRow])`` tuple per fact.  Uses
    two queries per batch (fact batch + IN-clause initiatives) — no N+1.

    Keyset pagination (``WHERE id > :last_id ORDER BY id LIMIT N``) is used
    instead of OFFSET for stability under concurrent writes.
    """
    base = _build_fact_select().where(Resource.application_id == application_id)

    last_id: UUID | None = None

    while True:
        if last_id is None:
            batch_stmt = base.order_by(AccessFact.id).limit(batch_size)
        else:
            batch_stmt = base.where(AccessFact.id > last_id).order_by(AccessFact.id).limit(batch_size)

        result = await session.execute(batch_stmt)
        rows = result.all()
        if not rows:
            break

        fact_rows = [_fact_row_from_row(r) for r in rows]
        fact_ids = [fr.id for fr in fact_rows]
        groups = await _fetch_initiatives_for_facts(session, fact_ids)

        for fact_row in fact_rows:
            yield fact_row, groups.get(fact_row.id, [])

        last_id = fact_ids[-1]
        if len(rows) < batch_size:
            break


async def upsert_effective_grants(
    session: AsyncSession,
    drafts: Sequence[EffectiveGrantDraft],
) -> UpsertResult:
    """Bulk-upsert EffectiveGrantDraft rows via ON CONFLICT on uq_effective_grants_source_pair.

    PostgreSQL 17 does not support system columns (xmax) in RETURNING clauses for
    partitioned tables.  Instead we pre-select existing source-pair tuples, execute
    the upsert, then compare returned ids to determine inserts vs updates.

    Returns a ``UpsertResult`` with exact counts.  Never swallows DB errors.
    """
    if not drafts:
        return UpsertResult(rows_upserted=0, rows_inserted=0, rows_updated=0, rows_tombstoned=0)

    # Step 1: collect source pairs that already exist
    source_pairs = [(draft.source_access_fact_id, draft.source_initiative_id) for draft in drafts]
    existing_result = await session.execute(
        sa.select(
            EffectiveGrant.source_access_fact_id,
            EffectiveGrant.source_initiative_id,
        ).where(
            sa.tuple_(
                EffectiveGrant.source_access_fact_id,
                EffectiveGrant.source_initiative_id,
            ).in_(source_pairs)
        )
    )
    existing_pairs: set[tuple[UUID, UUID]] = {
        (row.source_access_fact_id, row.source_initiative_id) for row in existing_result.all()
    }

    # Step 2: upsert
    values = [
        dict(
            id=uuid.uuid4(),
            subject_id=draft.subject_id,
            subject_kind=draft.subject_kind,
            application_id=draft.application_id,
            account_id=draft.account_id,
            resource_id=draft.resource_id,
            action=draft.action,
            effect=draft.effect,
            initiative_type=draft.initiative_type,
            initiative_origin=draft.initiative_origin,
            valid_from=draft.valid_from,
            valid_until=draft.valid_until,
            source_access_fact_id=draft.source_access_fact_id,
            source_initiative_id=draft.source_initiative_id,
            observed_at=draft.observed_at,
            tombstoned_at=draft.tombstoned_at,
        )
        for draft in drafts
    ]

    insert_stmt = pg_insert(EffectiveGrant).values(values)
    final_stmt = insert_stmt.on_conflict_do_update(
        constraint='uq_effective_grants_source_pair',
        set_=dict(
            account_id=insert_stmt.excluded.account_id,
            resource_id=insert_stmt.excluded.resource_id,
            action=insert_stmt.excluded.action,
            effect=insert_stmt.excluded.effect,
            initiative_type=insert_stmt.excluded.initiative_type,
            initiative_origin=insert_stmt.excluded.initiative_origin,
            valid_from=insert_stmt.excluded.valid_from,
            valid_until=insert_stmt.excluded.valid_until,
            observed_at=insert_stmt.excluded.observed_at,
            tombstoned_at=insert_stmt.excluded.tombstoned_at,
        ),
    ).returning(
        EffectiveGrant.id,
        EffectiveGrant.tombstoned_at,
        EffectiveGrant.source_access_fact_id,
        EffectiveGrant.source_initiative_id,
    )

    result = await session.execute(final_stmt)
    rows = result.all()

    rows_upserted = len(rows)
    rows_inserted = sum(1 for r in rows if (r.source_access_fact_id, r.source_initiative_id) not in existing_pairs)
    rows_updated = rows_upserted - rows_inserted
    rows_tombstoned = sum(1 for r in rows if r.tombstoned_at is not None)

    return UpsertResult(
        rows_upserted=rows_upserted,
        rows_inserted=rows_inserted,
        rows_updated=rows_updated,
        rows_tombstoned=rows_tombstoned,
    )


async def upsert_effective_grants_if_observed_at_newer(
    session: AsyncSession,
    drafts: Sequence[EffectiveGrantDraft],
    *,
    observed_at: datetime,
) -> UpsertResult:
    """Bulk-upsert with an ``observed_at`` compare-and-swap WHERE guard.

    Identical to ``upsert_effective_grants`` except the ``ON CONFLICT DO
    UPDATE`` branch carries ``WHERE effective_grants.observed_at <
    excluded.observed_at``. Under Postgres semantics, when that WHERE
    evaluates false the conflict row becomes an effective ``DO NOTHING``
    — the row is **not** included in RETURNING. This is the mechanism
    that makes the third bucket work: pre-SELECT existing source-pair
    set; after upsert, RETURNING rows minus existing-pairs = inserts,
    RETURNING rows intersected with existing-pairs = updates,
    existing-pairs minus RETURNING = rows_skipped (stale rows whose CAS
    guard rejected the update). Do not "optimize" away the pre-SELECT —
    without it, rows_skipped cannot be derived xmax-free. System columns
    in RETURNING remain banned (ARCH_CONTEXT invariant on partitioned
    tables).
    """
    if not drafts:
        return UpsertResult(rows_upserted=0, rows_inserted=0, rows_updated=0, rows_tombstoned=0, rows_skipped=0)

    # Step 1: collect source pairs that already exist
    source_pairs = [(draft.source_access_fact_id, draft.source_initiative_id) for draft in drafts]
    existing_result = await session.execute(
        sa.select(
            EffectiveGrant.source_access_fact_id,
            EffectiveGrant.source_initiative_id,
        ).where(
            sa.tuple_(
                EffectiveGrant.source_access_fact_id,
                EffectiveGrant.source_initiative_id,
            ).in_(source_pairs)
        )
    )
    existing_pairs: set[tuple[UUID, UUID]] = {
        (row.source_access_fact_id, row.source_initiative_id) for row in existing_result.all()
    }

    # Step 2: upsert with CAS guard
    values = [
        dict(
            id=uuid.uuid4(),
            subject_id=draft.subject_id,
            subject_kind=draft.subject_kind,
            application_id=draft.application_id,
            account_id=draft.account_id,
            resource_id=draft.resource_id,
            action=draft.action,
            effect=draft.effect,
            initiative_type=draft.initiative_type,
            initiative_origin=draft.initiative_origin,
            valid_from=draft.valid_from,
            valid_until=draft.valid_until,
            source_access_fact_id=draft.source_access_fact_id,
            source_initiative_id=draft.source_initiative_id,
            observed_at=draft.observed_at,
            tombstoned_at=draft.tombstoned_at,
        )
        for draft in drafts
    ]

    insert_stmt = pg_insert(EffectiveGrant).values(values)
    final_stmt = insert_stmt.on_conflict_do_update(
        constraint='uq_effective_grants_source_pair',
        set_=dict(
            account_id=insert_stmt.excluded.account_id,
            resource_id=insert_stmt.excluded.resource_id,
            action=insert_stmt.excluded.action,
            effect=insert_stmt.excluded.effect,
            initiative_type=insert_stmt.excluded.initiative_type,
            initiative_origin=insert_stmt.excluded.initiative_origin,
            valid_from=insert_stmt.excluded.valid_from,
            valid_until=insert_stmt.excluded.valid_until,
            observed_at=insert_stmt.excluded.observed_at,
            tombstoned_at=insert_stmt.excluded.tombstoned_at,
        ),
        where=EffectiveGrant.observed_at < insert_stmt.excluded.observed_at,
    ).returning(
        EffectiveGrant.id,
        EffectiveGrant.tombstoned_at,
        EffectiveGrant.source_access_fact_id,
        EffectiveGrant.source_initiative_id,
    )

    result = await session.execute(final_stmt)
    rows = result.all()

    returned_pairs: set[tuple[UUID, UUID]] = {(r.source_access_fact_id, r.source_initiative_id) for r in rows}
    rows_upserted = len(rows)
    rows_inserted = sum(1 for r in rows if (r.source_access_fact_id, r.source_initiative_id) not in existing_pairs)
    rows_updated = rows_upserted - rows_inserted
    rows_tombstoned = sum(1 for r in rows if r.tombstoned_at is not None)
    rows_skipped = sum(1 for pair in existing_pairs if pair not in returned_pairs)

    return UpsertResult(
        rows_upserted=rows_upserted,
        rows_inserted=rows_inserted,
        rows_updated=rows_updated,
        rows_tombstoned=rows_tombstoned,
        rows_skipped=rows_skipped,
    )


async def tombstone_effective_grants_for_access_fact(
    session: AsyncSession,
    *,
    access_fact_id: UUID,
    observed_at: datetime,
) -> int:
    """Idempotent, order-safe soft-delete of every grant sourced from this fact.

    ``UPDATE effective_grants SET tombstoned_at = :observed_at,
    observed_at = :observed_at WHERE source_access_fact_id = :id
    AND (tombstoned_at IS NULL OR tombstoned_at > :observed_at)
    AND observed_at < :observed_at``.
    Returns rowcount (0 = no-op). Never raises on "nothing to do".
    """
    stmt = (
        sa.update(EffectiveGrant)
        .where(
            EffectiveGrant.source_access_fact_id == access_fact_id,
            sa.or_(
                EffectiveGrant.tombstoned_at.is_(None),
                EffectiveGrant.tombstoned_at > observed_at,
            ),
            EffectiveGrant.observed_at < observed_at,
        )
        .values(tombstoned_at=observed_at, observed_at=observed_at)
    )
    result = await session.execute(stmt)
    return result.rowcount  # type: ignore[attr-defined,no-any-return]


async def tombstone_effective_grants_for_initiative(
    session: AsyncSession,
    *,
    initiative_id: UUID,
    observed_at: datetime,
) -> int:
    """Idempotent, order-safe soft-delete of every grant sourced from this initiative.

    ``UPDATE effective_grants SET tombstoned_at = :observed_at,
    observed_at = :observed_at WHERE source_initiative_id = :id
    AND (tombstoned_at IS NULL OR tombstoned_at > :observed_at)
    AND observed_at < :observed_at``.
    Returns rowcount (0 = no-op). Never raises on "nothing to do".
    """
    stmt = (
        sa.update(EffectiveGrant)
        .where(
            EffectiveGrant.source_initiative_id == initiative_id,
            sa.or_(
                EffectiveGrant.tombstoned_at.is_(None),
                EffectiveGrant.tombstoned_at > observed_at,
            ),
            EffectiveGrant.observed_at < observed_at,
        )
        .values(tombstoned_at=observed_at, observed_at=observed_at)
    )
    result = await session.execute(stmt)
    return result.rowcount  # type: ignore[attr-defined,no-any-return]


async def tombstone_effective_grants_for_missing_pairs(
    session: AsyncSession,
    *,
    access_fact_id: UUID,
    observed_at: datetime,
    live_initiative_ids: Collection[UUID],
) -> int:
    """Tombstone every grant for this fact whose initiative is NOT in the live set.

    Used by the UPSERT incremental-apply branch to close the "silent shrink"
    gap: when a reprojection builds a draft set that no longer contains an
    initiative the previous projection emitted, the corresponding
    ``effective_grants`` row must be tombstoned even though no
    ``initiative.expired`` event fired.

    SQL shape::

        UPDATE effective_grants
           SET tombstoned_at = :observed_at,
               observed_at   = :observed_at
         WHERE source_access_fact_id = :access_fact_id
           AND (<live-filter>)
           AND (tombstoned_at IS NULL OR tombstoned_at > :observed_at)
           AND observed_at < :observed_at

    ``<live-filter>`` is rendered:

    - ``source_initiative_id NOT IN (:ids)`` when ``live_initiative_ids`` is
      non-empty;
    - omitted (no predicate — all rows for the fact qualify) when
      ``live_initiative_ids`` is empty. Relying on SQLAlchemy's
      ``col.notin_([])`` is forbidden because its historical behaviour has
      differed across versions. Branch on Python-side
      ``len(live_initiative_ids) == 0``.

    Returns ``result.rowcount`` (0 on no-op). Never raises on empty work.
    """
    if live_initiative_ids:
        stmt = (
            sa.update(EffectiveGrant)
            .where(
                EffectiveGrant.source_access_fact_id == access_fact_id,
                EffectiveGrant.source_initiative_id.notin_(list(live_initiative_ids)),
                sa.or_(
                    EffectiveGrant.tombstoned_at.is_(None),
                    EffectiveGrant.tombstoned_at > observed_at,
                ),
                EffectiveGrant.observed_at < observed_at,
            )
            .values(tombstoned_at=observed_at, observed_at=observed_at)
        )
    else:
        stmt = (
            sa.update(EffectiveGrant)
            .where(
                EffectiveGrant.source_access_fact_id == access_fact_id,
                sa.or_(
                    EffectiveGrant.tombstoned_at.is_(None),
                    EffectiveGrant.tombstoned_at > observed_at,
                ),
                EffectiveGrant.observed_at < observed_at,
            )
            .values(tombstoned_at=observed_at, observed_at=observed_at)
        )
    result = await session.execute(stmt)
    return result.rowcount  # type: ignore[attr-defined,no-any-return]


# ---------------------------------------------------------------------------
# Step 4 — Read helpers
# ---------------------------------------------------------------------------


async def list_effective_grants(
    session: AsyncSession,
    *,
    subject_id: UUID | None = None,
    subject_kind: SubjectKind | None = None,
    application_id: UUID | None = None,
    account_id: UUID | None = None,
    resource_id: UUID | None = None,
    action: Action | None = None,
    effect: EffectiveGrantEffect | None = None,
    initiative_type: InitiativeType | None = None,
    initiative_origin: str | None = None,
    source_initiative_id: UUID | None = None,
    active_only: bool = True,
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
) -> list[EffectiveGrant]:
    """Return effective grants matching the given filters.

    ``active_only=True`` (default) applies::

        tombstoned_at IS NULL AND (valid_until IS NULL OR valid_until > now)

    Results are ordered ``observed_at DESC, id DESC`` for stable pagination.
    """
    _now = now if now is not None else datetime.now(UTC)
    conditions: list[sa.ColumnElement] = []  # type: ignore[type-arg]

    if subject_id is not None:
        conditions.append(EffectiveGrant.subject_id == subject_id)
    if subject_kind is not None:
        conditions.append(EffectiveGrant.subject_kind == subject_kind)
    if application_id is not None:
        conditions.append(EffectiveGrant.application_id == application_id)
    if account_id is not None:
        conditions.append(EffectiveGrant.account_id == account_id)
    if resource_id is not None:
        conditions.append(EffectiveGrant.resource_id == resource_id)
    if action is not None:
        conditions.append(EffectiveGrant.action == action)
    if effect is not None:
        conditions.append(EffectiveGrant.effect == effect)
    if initiative_type is not None:
        conditions.append(EffectiveGrant.initiative_type == initiative_type)
    if initiative_origin is not None:
        conditions.append(EffectiveGrant.initiative_origin == initiative_origin)
    if source_initiative_id is not None:
        conditions.append(EffectiveGrant.source_initiative_id == source_initiative_id)
    if active_only:
        conditions.append(EffectiveGrant.tombstoned_at.is_(None))
        conditions.append((EffectiveGrant.valid_until.is_(None)) | (EffectiveGrant.valid_until > _now))

    stmt = (
        sa.select(EffectiveGrant)
        .where(*conditions)
        .order_by(EffectiveGrant.observed_at.desc(), EffectiveGrant.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_effective_grant(
    session: AsyncSession,
    grant_id: UUID,
) -> EffectiveGrant | None:
    """Fetch a single EffectiveGrant row by id.

    .. note::
        The primary key is 3-column ``(id, subject_kind, application_id)``.  A
        lookup by ``id`` alone must scan all 12 partitions (3 kinds × 4 hash
        buckets each).  This is acceptable for admin/debug traffic.
        **Do not call from a hot path.**
    """
    stmt = sa.select(EffectiveGrant).where(EffectiveGrant.id == grant_id).limit(1)
    result = await session.execute(stmt)
    return result.scalars().one_or_none()


async def find_grants_for_access(
    session: AsyncSession,
    *,
    subject_id: UUID,
    resource_id: UUID,
    action: Action,
    active_only: bool = True,
    now: datetime | None = None,
) -> list[EffectiveGrant]:
    """Return all grants matching ``(subject_id, resource_id, action)``.

    Used by the ``/explain`` endpoint to surface every initiative that
    contributes to the subject's access to the given resource + action pair.
    The result set is bounded by the number of initiatives the subject holds
    for this entitlement — realistic ceiling is single-digit to low-tens, so
    no ``LIMIT`` is applied.
    """
    _now = now if now is not None else datetime.now(UTC)
    conditions: list[sa.ColumnElement] = [  # type: ignore[type-arg]
        EffectiveGrant.subject_id == subject_id,
        EffectiveGrant.resource_id == resource_id,
        EffectiveGrant.action == action,
    ]
    if active_only:
        conditions.append(EffectiveGrant.tombstoned_at.is_(None))
        conditions.append((EffectiveGrant.valid_until.is_(None)) | (EffectiveGrant.valid_until > _now))

    stmt = (
        sa.select(EffectiveGrant)
        .where(*conditions)
        .order_by(EffectiveGrant.observed_at.desc(), EffectiveGrant.id.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
