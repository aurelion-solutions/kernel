# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Action service — read-only access to the reference vocabulary."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.actions.models import Action
from src.inventory.actions.schemas import ActionRead
from src.platform.logs.service import LogService


class ActionService:
    """Read-only access to the Action reference vocabulary."""

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def list_actions(self) -> list[ActionRead]:
        """Return all Action rows ordered by id ascending."""
        stmt = select(Action).order_by(Action.id.asc())
        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [ActionRead.model_validate(row) for row in rows]

    async def get_action_by_slug(self, slug: str) -> ActionRead | None:
        """Return the Action with the given slug, or None if not found.

        Lookup is case-sensitive. The seeded vocabulary is lowercase by contract.
        """
        stmt = select(Action).where(Action.slug == slug)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return ActionRead.model_validate(row)
