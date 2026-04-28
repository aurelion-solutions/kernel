# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure database helpers for the lake_migration slice.

No logging, no events, no business logic.
``session.flush()`` only — caller owns the transaction boundary.
"""

from __future__ import annotations

import base64
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.lake_migration.models import (
    LakeMigrationDataset,
    LakeMigrationRun,
    LakeMigrationStatus,
)


async def get_run_by_id(
    session: AsyncSession,
    run_id: UUID,
) -> LakeMigrationRun | None:
    """Return run by id or None."""
    result = await session.execute(select(LakeMigrationRun).where(LakeMigrationRun.id == run_id))
    return result.scalar_one_or_none()


async def list_runs(
    session: AsyncSession,
    *,
    status_filter: LakeMigrationStatus | None = None,
    dataset_filter: LakeMigrationDataset | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[LakeMigrationRun], str | None]:
    """Cursor-paginated list of runs, ordered ``created_at DESC, id DESC``.

    Cursor token encodes ``<created_at_iso>|<id>`` as URL-safe base64.
    Returns ``(runs, next_cursor)``; ``next_cursor`` is ``None`` on last page.
    """
    stmt = select(LakeMigrationRun)

    if status_filter is not None:
        stmt = stmt.where(LakeMigrationRun.status == status_filter)
    if dataset_filter is not None:
        stmt = stmt.where(LakeMigrationRun.dataset == dataset_filter)

    if cursor is not None:
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts_str, id_str = decoded.split('|', 1)
            cursor_ts = datetime.fromisoformat(ts_str)
            cursor_id = UUID(id_str)
            stmt = stmt.where(
                (LakeMigrationRun.created_at < cursor_ts)
                | ((LakeMigrationRun.created_at == cursor_ts) & (LakeMigrationRun.id < cursor_id))
            )
        except Exception:  # noqa: BLE001
            pass

    stmt = stmt.order_by(
        LakeMigrationRun.created_at.desc(),
        LakeMigrationRun.id.desc(),
    ).limit(limit + 1)

    result = await session.execute(stmt)
    runs = list(result.scalars().all())

    next_cursor: str | None = None
    if len(runs) > limit:
        runs = runs[:limit]
        last = runs[-1]
        raw = f'{last.created_at.isoformat()}|{last.id}'
        next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()

    return runs, next_cursor
