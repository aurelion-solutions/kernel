# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Application repository."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.applications.models import Application


async def get_application_by_id(session: AsyncSession, application_id: uuid.UUID) -> Application | None:
    """Return application by id or None."""
    return await session.get(Application, application_id)


async def get_application_by_code(session: AsyncSession, code: str) -> Application | None:
    """Return application by its short stable code, or None."""
    result = await session.execute(select(Application).where(Application.code == code))
    return result.scalar_one_or_none()


async def list_applications(session: AsyncSession) -> list[Application]:
    """Return all applications ordered by name."""
    result = await session.execute(select(Application).order_by(Application.name.asc()))
    return list(result.scalars().all())
