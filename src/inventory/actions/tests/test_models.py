# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Model-level tests for the Action reference vocabulary.

Service test: not applicable — no service in this sub-step (Step 2b).
API test: not applicable — no routes in this sub-step (Step 2c).
CLI test: not applicable — no CLI in this sub-step (Step 2d).
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
import sqlalchemy.exc
from src.inventory.actions.models import Action


def test_tablename_is_ref_actions() -> None:
    assert Action.__tablename__ == 'ref_actions'


def test_has_expected_columns() -> None:
    columns = set(Action.__table__.columns.keys())
    assert columns == {'id', 'slug', 'description', 'created_at'}


def test_slug_column_is_unique() -> None:
    slug_col = Action.__table__.c.slug
    # SQLAlchemy may materialise unique=True as a column flag or as a
    # table-level UniqueConstraint — accept either shape.
    col_flag = bool(slug_col.unique)
    constraint_flag = any(
        isinstance(c, sa.UniqueConstraint) and list(c.columns) == [slug_col] for c in Action.__table__.constraints
    )
    assert col_flag or constraint_flag, (
        "Expected 'slug' column to have a unique constraint (column flag or table-level UniqueConstraint)"
    )


@pytest.mark.asyncio
async def test_slug_unique_constraint_enforced_at_db(session_factory) -> None:
    async with session_factory() as session:
        session.add(Action(slug='read', description='first'))
        await session.flush()

        session.add(Action(slug='read', description='duplicate'))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await session.flush()
