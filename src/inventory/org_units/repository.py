# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit repository for PostgreSQL access."""

from typing import Any
import uuid

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.org_units.models import OrgUnit


async def bulk_upsert_org_units_by_external_id(
    session: AsyncSession,
    triples: list[tuple[str, str, bool]],
) -> list[OrgUnit]:
    """Upsert org_units by external_id with parent_id=NULL (Pass 1).

    Args:
        session: SQLAlchemy async session.
        triples: list of (external_id, name, is_internal) tuples in input order.

    Returns:
        OrgUnit rows in the same order as triples.

    """
    if not triples:
        return []

    values = [
        {'external_id': ext_id, 'name': name, 'is_internal': is_internal} for ext_id, name, is_internal in triples
    ]

    insert_stmt = pg_insert(OrgUnit).values(values)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=['external_id'],
        set_={
            'name': insert_stmt.excluded.name,
            'is_internal': insert_stmt.excluded.is_internal,
        },
    ).returning(OrgUnit)

    result = await session.execute(stmt)
    rows: list[OrgUnit] = list(result.scalars().all())

    # Re-order to match input order — RETURNING order is not guaranteed.
    index: dict[str, OrgUnit] = {row.external_id: row for row in rows}
    return [index[ext_id] for ext_id, _, __ in triples]


async def update_parents_by_external_id(
    session: AsyncSession,
    mapping: dict[str, str],
) -> None:
    """Batch-update parent_id for org_units using a CASE...END statement.

    Pass 2 of the two-pass parent resolution algorithm. Issues a single
    UPDATE with a CASE expression — no N+1.

    Args:
        session: SQLAlchemy async session.
        mapping: {child_external_id: parent_id (UUID as str)} mapping.
            parent_id values are UUIDs converted to str for the CASE arms.

    """
    if not mapping:
        return

    child_external_ids = list(mapping.keys())
    case_expr = case(
        {ext_id: uuid.UUID(parent_id_str) for ext_id, parent_id_str in mapping.items()},
        value=OrgUnit.external_id,
    )

    stmt = update(OrgUnit).where(OrgUnit.external_id.in_(child_external_ids)).values(parent_id=case_expr)
    await session.execute(stmt)


async def get_by_external_ids(
    session: AsyncSession,
    external_ids: list[str],
) -> dict[str, uuid.UUID]:
    """Batch SELECT org_units by external_id. Returns {external_id -> id} mapping."""
    if not external_ids:
        return {}
    result = await session.execute(select(OrgUnit.id, OrgUnit.external_id).where(OrgUnit.external_id.in_(external_ids)))
    return {row.external_id: row.id for row in result}


async def create_org_unit(
    session: AsyncSession,
    *,
    external_id: str,
    name: str,
    description: str | None,
    is_internal: bool,
    parent_id: uuid.UUID | None,
) -> OrgUnit:
    """INSERT a single org_unit row and return it.

    Raises ``sqlalchemy.exc.IntegrityError`` on ``external_id`` uniqueness
    conflict; the caller (service) translates this to a domain exception.
    """
    org_unit = OrgUnit(
        external_id=external_id,
        name=name,
        description=description,
        is_internal=is_internal,
        parent_id=parent_id,
    )
    session.add(org_unit)
    await session.flush()
    await session.refresh(org_unit)
    return org_unit


async def get_org_unit(
    session: AsyncSession,
    org_unit_id: uuid.UUID,
) -> OrgUnit | None:
    """SELECT org_unit by primary key. Returns None if not found."""
    result = await session.execute(select(OrgUnit).where(OrgUnit.id == org_unit_id))
    return result.scalar_one_or_none()


async def update_org_unit(
    session: AsyncSession,
    org_unit_id: uuid.UUID,
    *,
    fields: dict[str, Any],
) -> OrgUnit | None:
    """UPDATE org_unit SET <fields> WHERE id = :id, return updated row or None."""
    if not fields:
        return await get_org_unit(session, org_unit_id)
    stmt = update(OrgUnit).where(OrgUnit.id == org_unit_id).values(**fields).returning(OrgUnit)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def delete_org_unit(
    session: AsyncSession,
    org_unit_id: uuid.UUID,
) -> bool:
    """DELETE org_unit WHERE id = :id. Returns True if a row was deleted."""
    stmt = delete(OrgUnit).where(OrgUnit.id == org_unit_id)
    result: CursorResult[Any] = await session.execute(stmt)  # type: ignore[assignment]
    return (result.rowcount or 0) > 0


async def list_org_units_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> tuple[list[OrgUnit], int]:
    """Return (rows, total) for paginated GET /org-units.

    Rows are ordered by external_id ASC and paginated by limit/offset.
    total is the unfiltered row count of org_units; if a filter is added
    later, total should reflect the count of matching rows.
    Two statements in one session — acceptable for this table size.
    """
    rows_result = await session.execute(select(OrgUnit).order_by(OrgUnit.external_id.asc()).limit(limit).offset(offset))
    rows = list(rows_result.scalars().all())

    count_result = await session.execute(select(func.count()).select_from(OrgUnit))
    total: int = count_result.scalar_one()

    return rows, total
