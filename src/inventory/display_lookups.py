# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Batch display-name lookup helpers.

Used by three read endpoints to resolve human-readable names from UUIDs
without N+1 queries.  Each public function does at most one SELECT per
entity type for the entire result set.

Layer rule: this module imports only from ``inventory/*`` models and
``platform/applications`` models — both Layer 1 peers.  It MUST NOT
import from engines or products.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.accounts.models import Account
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import NHI
from src.inventory.persons.models import Person
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject
from src.platform.applications.models import Application


@dataclass(frozen=True)
class ApplicationDisplay:
    """Holds both the short code and the full name for an Application."""

    code: str
    name: str


# ---------------------------------------------------------------------------
# Individual batched lookups
# ---------------------------------------------------------------------------


async def batch_employee_display(
    session: AsyncSession,
    employee_ids: set[UUID],
) -> dict[UUID, str]:
    """Return {employee_id: full_name} for the given set of employee UUIDs.

    Joins employees → persons to get full_name.  Missing IDs are silently
    omitted (caller maps missing → None).
    """
    if not employee_ids:
        return {}

    stmt = (
        sa.select(Employee.id, Person.full_name)
        .join(Person, Person.id == Employee.person_id)
        .where(Employee.id.in_(employee_ids))
    )
    result = await session.execute(stmt)
    return {row.id: row.full_name for row in result.all()}


async def batch_nhi_display(
    session: AsyncSession,
    nhi_ids: set[UUID],
) -> dict[UUID, str]:
    """Return {nhi_id: external_id} for the given set of NHI UUIDs."""
    if not nhi_ids:
        return {}

    stmt = sa.select(NHI.id, NHI.external_id).where(NHI.id.in_(nhi_ids))
    result = await session.execute(stmt)
    return {row.id: row.external_id for row in result.all()}


async def batch_account_display(
    session: AsyncSession,
    account_ids: set[UUID],
) -> dict[UUID, str]:
    """Return {account_id: username} for the given set of account UUIDs."""
    if not account_ids:
        return {}

    stmt = sa.select(Account.id, Account.username).where(Account.id.in_(account_ids))
    result = await session.execute(stmt)
    return {row.id: row.username for row in result.all()}


async def batch_resource_display(
    session: AsyncSession,
    resource_ids: set[UUID],
) -> dict[UUID, str]:
    """Return {resource_id: display_string} for the given set of resource UUIDs.

    Format: ``"external_id (kind)"`` when kind != 'misc', else ``"external_id"``.
    """
    if not resource_ids:
        return {}

    stmt = sa.select(Resource.id, Resource.external_id, Resource.kind).where(Resource.id.in_(resource_ids))
    result = await session.execute(stmt)
    out: dict[UUID, str] = {}
    for row in result.all():
        if row.kind and row.kind != 'misc':
            out[row.id] = f'{row.external_id} ({row.kind})'
        else:
            out[row.id] = row.external_id
    return out


async def batch_application_display(
    session: AsyncSession,
    application_ids: set[UUID],
) -> dict[UUID, ApplicationDisplay]:
    """Return {application_id: ApplicationDisplay(code, name)} for the given UUIDs.

    A single SELECT fetches both ``code`` and ``name`` so callers can show
    the full name (e.g. "GitHub Enterprise") while keeping the short code
    for fallback / legacy fields.
    """
    if not application_ids:
        return {}

    stmt = sa.select(Application.id, Application.code, Application.name).where(Application.id.in_(application_ids))
    result = await session.execute(stmt)
    return {row.id: ApplicationDisplay(code=row.code, name=row.name) for row in result.all()}


async def batch_application_display_by_code(
    session: AsyncSession,
    codes: set[str],
) -> dict[str, ApplicationDisplay]:
    """Return {application_code: ApplicationDisplay(code, name)} for the given short codes.

    Used when ``PlanItem.application`` stores a short code string (e.g. ``"GHE"``)
    rather than a UUID.  A single SELECT resolves all codes at once.
    Missing codes are silently omitted (caller maps missing → None).
    """
    if not codes:
        return {}

    stmt = sa.select(Application.id, Application.code, Application.name).where(Application.code.in_(codes))
    result = await session.execute(stmt)
    return {row.code: ApplicationDisplay(code=row.code, name=row.name) for row in result.all()}


# ---------------------------------------------------------------------------
# Composite subject display
# ---------------------------------------------------------------------------


async def batch_subject_display(
    session: AsyncSession,
    *,
    employee_ids: set[UUID],
    nhi_ids: set[UUID],
) -> dict[UUID, str]:
    """Return {id: display} merging employee full_names and NHI external_ids.

    Priority: employee lookup first, then NHI.  Caller may pass overlapping
    sets — this function handles them independently.
    """
    emp_map = await batch_employee_display(session, employee_ids)
    nhi_map = await batch_nhi_display(session, nhi_ids)
    return {**emp_map, **nhi_map}


async def batch_display_by_subject_ids(
    session: AsyncSession,
    subject_ids: set[UUID],
) -> dict[UUID, str]:
    """Return {subject.id: display_name} for the given Subject UUIDs.

    Resolves display via the subjects table which links to employees and NHIs:
    - employee subjects → persons.full_name
    - nhi subjects → nhis.external_id

    This is the correct lookup when callers hold ``subjects.id`` values
    (e.g. delta items, access facts) rather than raw ``employees.id`` /
    ``nhis.id`` values.
    """
    if not subject_ids:
        return {}

    # Employee subjects: JOIN subjects → employees → persons
    emp_stmt = (
        sa.select(Subject.id, Person.full_name)
        .join(Employee, Employee.id == Subject.principal_employee_id)
        .join(Person, Person.id == Employee.person_id)
        .where(Subject.id.in_(subject_ids))
    )
    emp_result = await session.execute(emp_stmt)
    emp_map: dict[UUID, str] = {row.id: row.full_name for row in emp_result.all()}

    # NHI subjects: JOIN subjects → nhis
    nhi_stmt = (
        sa.select(Subject.id, NHI.external_id)
        .join(NHI, NHI.id == Subject.principal_nhi_id)
        .where(Subject.id.in_(subject_ids))
    )
    nhi_result = await session.execute(nhi_stmt)
    nhi_map: dict[UUID, str] = {row.id: row.external_id for row in nhi_result.all()}

    return {**emp_map, **nhi_map}
