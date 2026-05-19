# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for single-row CRUD on org_units (Phase 20 M-A)."""

from typing import Any

import pytest
from src.inventory.org_units.schemas import OrgUnitBulkItem, OrgUnitCreate, OrgUnitUpdate
from src.inventory.org_units.service import (
    DuplicateExternalIdError,
    InternalOrgUnitImmutableError,
    OrgUnitNotFoundError,
    OrgUnitService,
    ParentMustBeExternalError,
)


@pytest.fixture
def svc() -> OrgUnitService:
    return OrgUnitService()


async def _seed_external(session_factory: Any, external_id: str, name: str) -> None:
    """Helper: seed a single external org-unit via the bulk service."""
    svc = OrgUnitService()
    item = OrgUnitBulkItem(external_id=external_id, name=name, is_internal=False)
    async with session_factory() as session:
        await svc.bulk_upsert_org_units(session, [item])
        await session.commit()


async def _seed_internal(session_factory: Any, external_id: str, name: str) -> None:
    """Helper: seed a single internal org-unit via the bulk service."""
    svc = OrgUnitService()
    item = OrgUnitBulkItem(external_id=external_id, name=name, is_internal=True)
    async with session_factory() as session:
        await svc.bulk_upsert_org_units(session, [item])
        await session.commit()


# ---------------------------------------------------------------------------
# create_external_org_unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_external_happy_path_no_parent(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Create an external org-unit with no parent — returns persisted row."""
    data = OrgUnitCreate(external_id='ma-ext-1', name='Vendor Corp', is_internal=False)
    async with session_factory() as session:
        ou = await svc.create_external_org_unit(session, data)
        await session.commit()

    assert ou.external_id == 'ma-ext-1'
    assert ou.name == 'Vendor Corp'
    assert ou.is_internal is False
    assert ou.parent_id is None
    assert ou.description is None


@pytest.mark.asyncio
async def test_create_external_with_description(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Create with description populates the column."""
    data = OrgUnitCreate(
        external_id='ma-ext-desc-1',
        name='Consulting Co',
        description='Main consulting partner',
        is_internal=False,
    )
    async with session_factory() as session:
        ou = await svc.create_external_org_unit(session, data)
        await session.commit()

    assert ou.description == 'Main consulting partner'


@pytest.mark.asyncio
async def test_create_external_with_valid_external_parent(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Create with a valid external parent_id resolves correctly."""
    await _seed_external(session_factory, 'ma-parent-ext', 'Parent Vendor')

    async with session_factory() as session:
        from src.inventory.org_units.repository import get_by_external_ids  # noqa: PLC0415

        id_map = await get_by_external_ids(session, ['ma-parent-ext'])
        parent_uuid = id_map['ma-parent-ext']

    data = OrgUnitCreate(
        external_id='ma-child-ext',
        name='Child Vendor',
        is_internal=False,
        parent_id=parent_uuid,
    )
    async with session_factory() as session:
        ou = await svc.create_external_org_unit(session, data)
        await session.commit()

    assert ou.parent_id == parent_uuid


@pytest.mark.asyncio
async def test_create_external_with_internal_parent_raises_422(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """parent_id pointing at an internal org-unit raises ParentMustBeExternalError."""
    await _seed_internal(session_factory, 'ma-internal-parent', 'Internal Dept')

    async with session_factory() as session:
        from src.inventory.org_units.repository import get_by_external_ids  # noqa: PLC0415

        id_map = await get_by_external_ids(session, ['ma-internal-parent'])
        internal_uuid = id_map['ma-internal-parent']

    data = OrgUnitCreate(
        external_id='ma-bad-child',
        name='Bad Child',
        is_internal=False,
        parent_id=internal_uuid,
    )
    with pytest.raises(ParentMustBeExternalError):
        async with session_factory() as session:
            await svc.create_external_org_unit(session, data)


@pytest.mark.asyncio
async def test_create_external_with_nonexistent_parent_raises_422(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """parent_id pointing at a non-existent row raises ParentMustBeExternalError."""
    import uuid  # noqa: PLC0415

    data = OrgUnitCreate(
        external_id='ma-ghost-child',
        name='Ghost Child',
        is_internal=False,
        parent_id=uuid.uuid4(),
    )
    with pytest.raises(ParentMustBeExternalError):
        async with session_factory() as session:
            await svc.create_external_org_unit(session, data)


@pytest.mark.asyncio
async def test_create_external_duplicate_external_id_raises_409(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Duplicate external_id on create translates IntegrityError to DuplicateExternalIdError."""
    data = OrgUnitCreate(external_id='ma-dup-1', name='First', is_internal=False)
    async with session_factory() as session:
        await svc.create_external_org_unit(session, data)
        await session.commit()

    with pytest.raises(DuplicateExternalIdError):
        async with session_factory() as session:
            await svc.create_external_org_unit(session, data)


# ---------------------------------------------------------------------------
# read_org_unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_org_unit_hit(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """read_org_unit returns the row when it exists."""
    data = OrgUnitCreate(external_id='ma-read-1', name='Readable Corp', is_internal=False)
    async with session_factory() as session:
        created = await svc.create_external_org_unit(session, data)
        await session.commit()
        ou_id = created.id

    async with session_factory() as session:
        row = await svc.read_org_unit(session, ou_id)
    assert row.id == ou_id


@pytest.mark.asyncio
async def test_read_org_unit_miss_raises_404(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """read_org_unit raises OrgUnitNotFoundError when the row does not exist."""
    import uuid  # noqa: PLC0415

    with pytest.raises(OrgUnitNotFoundError):
        async with session_factory() as session:
            await svc.read_org_unit(session, uuid.uuid4())


# ---------------------------------------------------------------------------
# update_external_org_unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_name_only(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Patching only name leaves description unchanged."""
    data = OrgUnitCreate(
        external_id='ma-upd-1',
        name='Old Name',
        description='Keep this',
        is_internal=False,
    )
    async with session_factory() as session:
        created = await svc.create_external_org_unit(session, data)
        await session.commit()
        ou_id = created.id

    patch = OrgUnitUpdate(name='New Name')
    async with session_factory() as session:
        updated = await svc.update_external_org_unit(session, ou_id, patch)
        await session.commit()

    assert updated.name == 'New Name'
    assert updated.description == 'Keep this'


@pytest.mark.asyncio
async def test_update_description_only(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Patching only description leaves name unchanged."""
    data = OrgUnitCreate(external_id='ma-upd-2', name='Stable Name', is_internal=False)
    async with session_factory() as session:
        created = await svc.create_external_org_unit(session, data)
        await session.commit()
        ou_id = created.id

    patch = OrgUnitUpdate(description='New description')
    async with session_factory() as session:
        updated = await svc.update_external_org_unit(session, ou_id, patch)
        await session.commit()

    assert updated.name == 'Stable Name'
    assert updated.description == 'New description'


@pytest.mark.asyncio
async def test_update_both_fields(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Patching both name and description updates both."""
    data = OrgUnitCreate(external_id='ma-upd-3', name='Old', is_internal=False)
    async with session_factory() as session:
        created = await svc.create_external_org_unit(session, data)
        await session.commit()
        ou_id = created.id

    patch = OrgUnitUpdate(name='New', description='Added desc')
    async with session_factory() as session:
        updated = await svc.update_external_org_unit(session, ou_id, patch)
        await session.commit()

    assert updated.name == 'New'
    assert updated.description == 'Added desc'


@pytest.mark.asyncio
async def test_update_empty_patch_no_op(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Empty patch (no fields set) returns the row unchanged."""
    data = OrgUnitCreate(
        external_id='ma-upd-noop',
        name='Unchanged',
        description='Unchanged desc',
        is_internal=False,
    )
    async with session_factory() as session:
        created = await svc.create_external_org_unit(session, data)
        await session.commit()
        ou_id = created.id

    patch = OrgUnitUpdate()
    async with session_factory() as session:
        updated = await svc.update_external_org_unit(session, ou_id, patch)

    assert updated.name == 'Unchanged'
    assert updated.description == 'Unchanged desc'


@pytest.mark.asyncio
async def test_update_internal_row_raises_409(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Updating an internal org-unit raises InternalOrgUnitImmutableError."""
    await _seed_internal(session_factory, 'ma-int-upd', 'Internal Dept')

    async with session_factory() as session:
        from src.inventory.org_units.repository import get_by_external_ids  # noqa: PLC0415

        id_map = await get_by_external_ids(session, ['ma-int-upd'])
        int_id = id_map['ma-int-upd']

    patch = OrgUnitUpdate(name='Attempted rename')
    with pytest.raises(InternalOrgUnitImmutableError):
        async with session_factory() as session:
            await svc.update_external_org_unit(session, int_id, patch)


@pytest.mark.asyncio
async def test_update_missing_row_raises_404(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Updating a non-existent row raises OrgUnitNotFoundError."""
    import uuid  # noqa: PLC0415

    patch = OrgUnitUpdate(name='Ghost')
    with pytest.raises(OrgUnitNotFoundError):
        async with session_factory() as session:
            await svc.update_external_org_unit(session, uuid.uuid4(), patch)


# ---------------------------------------------------------------------------
# delete_external_org_unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_external_row(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Deleting an external org-unit removes it; subsequent read raises 404."""
    data = OrgUnitCreate(external_id='ma-del-1', name='Deletable', is_internal=False)
    async with session_factory() as session:
        created = await svc.create_external_org_unit(session, data)
        await session.commit()
        ou_id = created.id

    async with session_factory() as session:
        await svc.delete_external_org_unit(session, ou_id)
        await session.commit()

    with pytest.raises(OrgUnitNotFoundError):
        async with session_factory() as session:
            await svc.read_org_unit(session, ou_id)


@pytest.mark.asyncio
async def test_delete_internal_row_raises_409(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Deleting an internal org-unit raises InternalOrgUnitImmutableError."""
    await _seed_internal(session_factory, 'ma-int-del', 'Internal Dept Del')

    async with session_factory() as session:
        from src.inventory.org_units.repository import get_by_external_ids  # noqa: PLC0415

        id_map = await get_by_external_ids(session, ['ma-int-del'])
        int_id = id_map['ma-int-del']

    with pytest.raises(InternalOrgUnitImmutableError):
        async with session_factory() as session:
            await svc.delete_external_org_unit(session, int_id)


@pytest.mark.asyncio
async def test_delete_missing_row_raises_404(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Deleting a non-existent row raises OrgUnitNotFoundError."""
    import uuid  # noqa: PLC0415

    with pytest.raises(OrgUnitNotFoundError):
        async with session_factory() as session:
            await svc.delete_external_org_unit(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_delete_cascades_employee_org_unit_id_to_null(
    svc: OrgUnitService,
    session_factory: Any,
) -> None:
    """Deleting an org-unit sets bound employees' org_unit_id to NULL (FK SET NULL)."""
    import uuid as uuid_module  # noqa: PLC0415

    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.employees.models import Employee  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    # Seed an external org-unit.
    data = OrgUnitCreate(external_id='ma-del-cascade', name='Cascade Corp', is_internal=False)
    async with session_factory() as session:
        ou = await svc.create_external_org_unit(session, data)
        await session.commit()
        ou_id = ou.id

    # Seed a Person then an Employee bound to this org-unit.
    async with session_factory() as session:
        person = Person(
            id=uuid_module.uuid4(),
            external_id='ma-person-cascade',
            full_name='Alice Cascade',
        )
        session.add(person)
        await session.flush()
        emp = Employee(
            id=uuid_module.uuid4(),
            person_id=person.id,
            org_unit_id=ou_id,
        )
        session.add(emp)
        await session.commit()
        emp_id = emp.id

    # Delete the org-unit.
    async with session_factory() as session:
        await svc.delete_external_org_unit(session, ou_id)
        await session.commit()

    # Employee row still exists but org_unit_id is NULL.
    async with session_factory() as session:
        result = await session.execute(sa.select(Employee).where(Employee.id == emp_id))
        emp_row = result.scalar_one()
    assert emp_row is not None
    assert emp_row.org_unit_id is None
