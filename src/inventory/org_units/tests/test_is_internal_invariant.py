# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the org_units.is_internal per-tree consistency trigger.

The trigger ``trg_org_units_is_internal_consistency`` fires
``BEFORE INSERT OR UPDATE OF parent_id, is_internal`` and enforces that every
node in a connected org-unit tree shares the same ``is_internal`` value.

Subtree flips of >1 node are NOT supported via plain UPDATE — any single-row
UPDATE that would make a node's ``is_internal`` disagree with its parent or
any child is rejected.  To convert a multi-node subtree, drop it and recreate
it with the new value.

These tests exercise the trigger directly via the repository layer (bypassing
OrgUnitService) so that DBAPIError propagates without service-layer wrapping.
"""

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from src.inventory.org_units.models import OrgUnit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_unit(
    session: Any,
    external_id: str,
    name: str,
    *,
    is_internal: bool = True,
    parent_id: Any = None,
) -> OrgUnit:
    """Insert a single OrgUnit and flush (no commit — caller decides TX boundary)."""
    unit = OrgUnit(
        external_id=external_id,
        name=name,
        is_internal=is_internal,
        parent_id=parent_id,
    )
    session.add(unit)
    await session.flush()
    return unit


def _assert_check_violation(exc_info: pytest.ExceptionInfo[DBAPIError]) -> None:
    """Assert the DBAPIError is a PostgreSQL check_violation (SQLSTATE 23514)."""
    orig = exc_info.value.orig
    sqlstate = getattr(orig, 'sqlstate', None) or getattr(orig, 'pgcode', None)
    assert sqlstate == '23514', f'Expected SQLSTATE 23514, got {sqlstate!r}: {exc_info.value}'


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_with_mismatched_is_internal_rejected(
    session_factory: Any,
) -> None:
    """Insert parent is_internal=True, then try child is_internal=False → rejected."""
    async with session_factory() as session:
        parent = await _insert_unit(session, 'inv-parent-1', 'Parent', is_internal=True)
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as session:
            await _insert_unit(
                session,
                'inv-child-bad-1',
                'Bad Child',
                is_internal=False,
                parent_id=parent.id,
            )
            await session.commit()

    _assert_check_violation(exc_info)


@pytest.mark.asyncio
async def test_flipping_parent_with_existing_children_rejected(
    session_factory: Any,
) -> None:
    """Parent + child both True; UPDATE parent is_internal=False → rejected."""
    async with session_factory() as session:
        parent = await _insert_unit(session, 'inv-parent-flip', 'Parent', is_internal=True)
        await session.flush()
        await _insert_unit(
            session,
            'inv-child-flip',
            'Child',
            is_internal=True,
            parent_id=parent.id,
        )
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as session:
            await session.execute(
                sa.update(OrgUnit).where(OrgUnit.external_id == 'inv-parent-flip').values(is_internal=False)
            )
            await session.commit()

    _assert_check_violation(exc_info)


@pytest.mark.asyncio
async def test_flipping_child_is_internal_with_intact_parent_link(
    session_factory: Any,
) -> None:
    """Parent True, child True; UPDATE child is_internal=False (parent unchanged) → rejected.

    This is the critical case that proves the BLOCKING trigger fix landed:
    the parent-side check must fire on every UPDATE of a node that has a
    parent, not only when parent_id itself changes.
    """
    async with session_factory() as session:
        parent = await _insert_unit(session, 'leaf-flip-parent', 'Parent', is_internal=True)
        await session.flush()
        await _insert_unit(
            session,
            'leaf-flip-child',
            'Child',
            is_internal=True,
            parent_id=parent.id,
        )
        await session.commit()

    # UPDATE child's is_internal only — parent_id is not touched.
    # Under the fixed trigger this must be rejected (child would disagree
    # with the still-True parent).
    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as session:
            await session.execute(
                sa.update(OrgUnit).where(OrgUnit.external_id == 'leaf-flip-child').values(is_internal=False)
            )
            await session.commit()

    _assert_check_violation(exc_info)


@pytest.mark.asyncio
async def test_reparent_to_same_kind_tree_accepted(
    session_factory: Any,
) -> None:
    """Move node A (is_internal=True) under node B (is_internal=True) → accepted."""
    async with session_factory() as session:
        b = await _insert_unit(session, 'rp-same-b', 'B', is_internal=True)
        await session.flush()
        await _insert_unit(session, 'rp-same-a', 'A', is_internal=True)
        await session.commit()

    async with session_factory() as session:
        await session.execute(sa.update(OrgUnit).where(OrgUnit.external_id == 'rp-same-a').values(parent_id=b.id))
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(sa.select(OrgUnit.parent_id).where(OrgUnit.external_id == 'rp-same-a'))
        row = result.scalar_one()
    assert row == b.id


@pytest.mark.asyncio
async def test_reparent_to_different_kind_tree_rejected(
    session_factory: Any,
) -> None:
    """Move node A (is_internal=True) under B (is_internal=False) → rejected."""
    async with session_factory() as session:
        b = await _insert_unit(session, 'rp-diff-b', 'B External', is_internal=False)
        await session.flush()
        await _insert_unit(session, 'rp-diff-a', 'A Internal', is_internal=True)
        await session.commit()

    with pytest.raises(DBAPIError) as exc_info:
        async with session_factory() as session:
            await session.execute(sa.update(OrgUnit).where(OrgUnit.external_id == 'rp-diff-a').values(parent_id=b.id))
            await session.commit()

    _assert_check_violation(exc_info)


@pytest.mark.asyncio
async def test_root_flip_with_no_children_accepted(
    session_factory: Any,
) -> None:
    """Root row with no children, no parent → UPDATE is_internal=False accepted."""
    async with session_factory() as session:
        await _insert_unit(session, 'solo-root', 'Solo Root', is_internal=True)
        await session.commit()

    async with session_factory() as session:
        await session.execute(sa.update(OrgUnit).where(OrgUnit.external_id == 'solo-root').values(is_internal=False))
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(sa.select(OrgUnit.is_internal).where(OrgUnit.external_id == 'solo-root'))
        val = result.scalar_one()
    assert val is False


@pytest.mark.asyncio
async def test_update_unrelated_column_does_not_fire_trigger(
    session_factory: Any,
) -> None:
    """UPDATE name (not parent_id or is_internal) does NOT raise trigger check.

    Verifies the ``OF parent_id, is_internal`` column-filter clause on the
    trigger prevents spurious executions.  We use a parent+child pair that
    would fail the trigger check if it fired, to confirm the trigger is truly
    not invoked on name-only updates.
    """
    async with session_factory() as session:
        parent = await _insert_unit(session, 'name-upd-parent', 'Parent', is_internal=True)
        await session.flush()
        await _insert_unit(
            session,
            'name-upd-child',
            'Child',
            is_internal=True,
            parent_id=parent.id,
        )
        await session.commit()

    # Rename the child — trigger must NOT fire (column filter).
    async with session_factory() as session:
        await session.execute(
            sa.update(OrgUnit).where(OrgUnit.external_id == 'name-upd-child').values(name='Child Renamed')
        )
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(sa.select(OrgUnit.name).where(OrgUnit.external_id == 'name-upd-child'))
        assert result.scalar_one() == 'Child Renamed'
