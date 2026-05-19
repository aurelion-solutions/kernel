# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person repository for PostgreSQL access."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.persons.models import Person, PersonAttribute


async def create_person(
    session: AsyncSession,
    *,
    external_id: str,
    full_name: str,
) -> Person:
    """Create and persist a person."""
    person = Person(external_id=external_id, full_name=full_name)
    session.add(person)
    await session.flush()
    await session.refresh(person)
    return person


async def get_person_by_id(
    session: AsyncSession,
    person_id: uuid.UUID,
) -> Person | None:
    """Load person by id."""
    result = await session.execute(select(Person).where(Person.id == person_id))
    return result.scalar_one_or_none()


async def get_person_by_external_id(
    session: AsyncSession,
    external_id: str,
) -> Person | None:
    """Load person by external_id."""
    result = await session.execute(select(Person).where(Person.external_id == external_id))
    return result.scalar_one_or_none()


async def list_persons_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> tuple[list[Person], int]:
    """Return (rows, total) for paginated GET /persons.

    Rows are ordered by external_id ASC and paginated by limit/offset.
    total is the unfiltered row count.
    """
    rows_result = await session.execute(select(Person).order_by(Person.external_id.asc()).limit(limit).offset(offset))
    rows = list(rows_result.scalars().all())

    count_result = await session.execute(select(func.count()).select_from(Person))
    total: int = count_result.scalar_one()

    return rows, total


async def list_person_attributes(
    session: AsyncSession,
    person_id: uuid.UUID,
) -> list[PersonAttribute]:
    """List attributes for a person."""
    result = await session.execute(
        select(PersonAttribute).where(PersonAttribute.person_id == person_id).order_by(PersonAttribute.key)
    )
    return list(result.scalars().all())


async def create_person_attribute(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    key: str,
    value: str,
) -> PersonAttribute:
    """Create and persist a person attribute."""
    attr = PersonAttribute(person_id=person_id, key=key, value=value)
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def get_person_attribute_by_key(
    session: AsyncSession,
    person_id: uuid.UUID,
    key: str,
) -> PersonAttribute | None:
    """Load person attribute by person_id and key."""
    result = await session.execute(
        select(PersonAttribute).where(
            PersonAttribute.person_id == person_id,
            PersonAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def delete_person_attribute(
    session: AsyncSession,
    person_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete person attribute by person_id and key. Returns True if deleted."""
    attr = await get_person_attribute_by_key(session, person_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True


async def bulk_upsert_persons(
    session: AsyncSession,
    items: list[tuple[str, str]],
) -> list[Person]:
    """Upsert persons by external_id.

    Args:
        session: SQLAlchemy async session.
        items: list of (external_id, full_name) tuples, in input order.

    Returns:
        Persons in the same order as items.

    """
    if not items:
        return []

    values = [{'external_id': ext_id, 'full_name': name} for ext_id, name in items]

    insert_stmt = pg_insert(Person).values(values)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=['external_id'],
        set_={'full_name': insert_stmt.excluded.full_name},
    ).returning(Person)

    result = await session.execute(stmt)
    rows: list[Person] = list(result.scalars().all())

    # Re-order to match input order — RETURNING order is not guaranteed.
    index: dict[str, Person] = {row.external_id: row for row in rows}
    return [index[ext_id] for ext_id, _ in items]
