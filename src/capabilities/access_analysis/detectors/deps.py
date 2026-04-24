# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for detector services."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.detectors.service import (
    OrphanDetectorService,
    TerminatedDetectorService,
    UnusedDetectorService,
)
from src.core.db.deps import get_db

_DependsDB = Depends(get_db)


async def get_orphan_detector_service(
    session: AsyncSession = _DependsDB,
) -> OrphanDetectorService:
    """Return an OrphanDetectorService bound to the request session."""
    return OrphanDetectorService(session)


async def get_terminated_detector_service(
    session: AsyncSession = _DependsDB,
) -> TerminatedDetectorService:
    """Return a TerminatedDetectorService bound to the request session."""
    return TerminatedDetectorService(session)


async def get_unused_detector_service(
    session: AsyncSession = _DependsDB,
) -> UnusedDetectorService:
    """Return an UnusedDetectorService bound to the request session."""
    return UnusedDetectorService(session)
