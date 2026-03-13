# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Role repository."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.roles.models import Role


async def list_by_application(
    session: AsyncSession,
    application_id: uuid.UUID,
) -> list[Role]:
    """Load all roles for the given application."""
    result = await session.execute(select(Role).where(Role.application_id == application_id))
    return list(result.scalars().all())
