# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure DB query helpers for the LLM model and execution-profile CRUD paths."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.llm.models import LLMExecutionProfile, LLMModel


async def get_by_id(session: AsyncSession, model_id: uuid.UUID) -> LLMModel | None:
    """Return an LLMModel by primary key, or None."""
    return await session.get(LLMModel, model_id)


async def get_by_name(session: AsyncSession, name: str) -> LLMModel | None:
    """Return an LLMModel by unique name, or None."""
    result = await session.execute(select(LLMModel).where(LLMModel.name == name))
    return result.scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[LLMModel]:
    """Return all LLMModel rows sorted by name ascending."""
    result = await session.execute(select(LLMModel).order_by(LLMModel.name.asc()))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# LLMExecutionProfile query helpers
# ---------------------------------------------------------------------------


async def get_profile_by_id(
    session: AsyncSession,
    profile_id: uuid.UUID,
) -> LLMExecutionProfile | None:
    """Return an LLMExecutionProfile by primary key, or None."""
    return await session.get(LLMExecutionProfile, profile_id)


async def get_profile_by_name(
    session: AsyncSession,
    name: str,
) -> LLMExecutionProfile | None:
    """Return an LLMExecutionProfile by unique name, or None."""
    result = await session.execute(select(LLMExecutionProfile).where(LLMExecutionProfile.name == name))
    return result.scalar_one_or_none()


async def list_profiles(session: AsyncSession) -> list[LLMExecutionProfile]:
    """Return all LLMExecutionProfile rows sorted by name ascending."""
    result = await session.execute(select(LLMExecutionProfile).order_by(LLMExecutionProfile.name.asc()))
    return list(result.scalars().all())
