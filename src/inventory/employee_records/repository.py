# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmployeeRecord repository for PostgreSQL access."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employee_records.models import (
    EmployeeProviderAttributeMapping,
    EmployeeRecord,
    EmployeeRecordAttribute,
    EmployeeRecordMatch,
)


async def create_employee_record(
    session: AsyncSession,
    *,
    external_id: str,
    application_id: uuid.UUID,
    description: str | None = None,
) -> EmployeeRecord:
    """Create and persist an employee record."""
    record = EmployeeRecord(
        external_id=external_id,
        application_id=application_id,
        description=description,
    )
    session.add(record)
    await session.flush()
    await session.refresh(record)
    return record


async def get_employee_record_by_id(
    session: AsyncSession,
    employee_record_id: uuid.UUID,
) -> EmployeeRecord | None:
    """Load employee record by id."""
    result = await session.execute(select(EmployeeRecord).where(EmployeeRecord.id == employee_record_id))
    return result.scalar_one_or_none()


async def get_employee_record_by_external_id(
    session: AsyncSession,
    external_id: str,
    application_id: uuid.UUID,
) -> EmployeeRecord | None:
    """Load employee record by external_id and application_id."""
    result = await session.execute(
        select(EmployeeRecord).where(
            EmployeeRecord.external_id == external_id,
            EmployeeRecord.application_id == application_id,
        )
    )
    return result.scalar_one_or_none()


async def list_employee_records(session: AsyncSession) -> list[EmployeeRecord]:
    """List all employee records."""
    result = await session.execute(select(EmployeeRecord).order_by(EmployeeRecord.external_id))
    return list(result.scalars().all())


async def list_employee_record_attributes(
    session: AsyncSession,
    employee_record_id: uuid.UUID,
) -> list[EmployeeRecordAttribute]:
    """List attributes for an employee record."""
    result = await session.execute(
        select(EmployeeRecordAttribute)
        .where(EmployeeRecordAttribute.employee_record_id == employee_record_id)
        .order_by(EmployeeRecordAttribute.key)
    )
    return list(result.scalars().all())


async def create_employee_record_attribute(
    session: AsyncSession,
    *,
    employee_record_id: uuid.UUID,
    key: str,
    value: str,
) -> EmployeeRecordAttribute:
    """Create and persist an employee record attribute."""
    attr = EmployeeRecordAttribute(
        employee_record_id=employee_record_id,
        key=key,
        value=value,
    )
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def get_employee_record_attribute_by_key(
    session: AsyncSession,
    employee_record_id: uuid.UUID,
    key: str,
) -> EmployeeRecordAttribute | None:
    """Load employee record attribute by employee_record_id and key."""
    result = await session.execute(
        select(EmployeeRecordAttribute).where(
            EmployeeRecordAttribute.employee_record_id == employee_record_id,
            EmployeeRecordAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def delete_employee_record_attribute(
    session: AsyncSession,
    employee_record_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete employee record attribute by employee_record_id and key. Returns True if deleted."""
    attr = await get_employee_record_attribute_by_key(session, employee_record_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True


async def list_provider_attribute_mappings_for_application(
    session: AsyncSession,
    application_id: uuid.UUID,
    *,
    is_determinator: bool | None = None,
    allow_upstream: bool | None = None,
) -> list[EmployeeProviderAttributeMapping]:
    """List provider attribute mappings for an application with optional filters."""
    stmt = select(EmployeeProviderAttributeMapping).where(
        EmployeeProviderAttributeMapping.application_id == application_id
    )
    if is_determinator is not None:
        stmt = stmt.where(EmployeeProviderAttributeMapping.is_determinator == is_determinator)
    if allow_upstream is not None:
        stmt = stmt.where(EmployeeProviderAttributeMapping.allow_upstream == allow_upstream)
    stmt = stmt.order_by(EmployeeProviderAttributeMapping.employee_record_key)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_employee_record_ids_with_attribute_key_value(
    session: AsyncSession,
    *,
    key: str,
    value: str,
    exclude_employee_record_id: uuid.UUID,
) -> list[uuid.UUID]:
    """Peer records sharing the same attribute key/value (excluding one record)."""
    result = await session.execute(
        select(EmployeeRecordAttribute.employee_record_id)
        .where(
            EmployeeRecordAttribute.key == key,
            EmployeeRecordAttribute.value == value,
            EmployeeRecordAttribute.employee_record_id != exclude_employee_record_id,
        )
        .order_by(EmployeeRecordAttribute.employee_record_id)
    )
    return list(result.scalars().all())


async def get_employee_record_match_by_record_id(
    session: AsyncSession,
    employee_record_id: uuid.UUID,
) -> EmployeeRecordMatch | None:
    """Load match row for an employee record, if any."""
    result = await session.execute(
        select(EmployeeRecordMatch).where(EmployeeRecordMatch.employee_record_id == employee_record_id)
    )
    return result.scalar_one_or_none()


async def upsert_employee_record_match(
    session: AsyncSession,
    *,
    employee_record_id: uuid.UUID,
    employee_id: uuid.UUID,
    matched_via_determinator: bool,
) -> EmployeeRecordMatch:
    """Create or replace the match for an employee record."""
    existing = await get_employee_record_match_by_record_id(session, employee_record_id)
    if existing is not None:
        existing.employee_id = employee_id
        existing.matched_via_determinator = matched_via_determinator
        await session.flush()
        await session.refresh(existing)
        return existing
    match = EmployeeRecordMatch(
        employee_record_id=employee_record_id,
        employee_id=employee_id,
        matched_via_determinator=matched_via_determinator,
    )
    session.add(match)
    await session.flush()
    await session.refresh(match)
    return match
