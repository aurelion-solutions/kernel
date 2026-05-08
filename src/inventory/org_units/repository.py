# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit repository for PostgreSQL access."""

import uuid

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.org_units.models import OrgUnit


async def bulk_upsert_org_units_by_external_id(
    session: AsyncSession,
    pairs: list[tuple[str, str]],
) -> list[OrgUnit]:
    """Upsert org_units by external_id with parent_id=NULL (Pass 1).

    Args:
        session: SQLAlchemy async session.
        pairs: list of (external_id, name) tuples in input order.

    Returns:
        OrgUnit rows in the same order as pairs.

    """
    if not pairs:
        return []

    values = [{'external_id': ext_id, 'name': name} for ext_id, name in pairs]

    insert_stmt = pg_insert(OrgUnit).values(values)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=['external_id'],
        set_={'name': insert_stmt.excluded.name},
    ).returning(OrgUnit)

    result = await session.execute(stmt)
    rows: list[OrgUnit] = list(result.scalars().all())

    # Re-order to match input order — RETURNING order is not guaranteed.
    index: dict[str, OrgUnit] = {row.external_id: row for row in rows}
    return [index[ext_id] for ext_id, _ in pairs]


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


async def list_all_org_units(session: AsyncSession) -> list[OrgUnit]:
    """SELECT all org_units ordered by external_id ASC."""
    result = await session.execute(select(OrgUnit).order_by(OrgUnit.external_id.asc()))
    return list(result.scalars().all())
