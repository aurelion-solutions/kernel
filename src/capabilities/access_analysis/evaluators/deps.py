# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependency for SodEvaluatorService."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.evaluators.service import SodEvaluatorService
from src.core.db.deps import get_db

_DependsDB = Depends(get_db)


async def get_sod_evaluator_service(
    session: AsyncSession = _DependsDB,
) -> SodEvaluatorService:
    """Return a SodEvaluatorService bound to the request session."""
    return SodEvaluatorService(session)
