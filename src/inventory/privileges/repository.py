# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Privilege repository."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.privileges.models import Privilege


async def list_by_application(
    session: AsyncSession,
    application_id: uuid.UUID,
) -> list[Privilege]:
    """Load all privileges for the given application."""
    result = await session.execute(select(Privilege).where(Privilege.application_id == application_id))
    return list(result.scalars().all())
