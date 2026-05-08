# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependency providers for the Effective Access read API.

Only ``get_effective_access_read_service`` is defined here.  The write-side
``EffectiveAccessProjectionService`` is constructed inline by its caller
(event consumer / admin trigger — not yet shipped) and does not need a
FastAPI dependency factory at this phase.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.effective_access.service import EffectiveAccessReadService

_DependsDB = Depends(get_db)


def get_effective_access_read_service(
    session: AsyncSession = _DependsDB,
) -> EffectiveAccessReadService:
    """Return an ``EffectiveAccessReadService`` bound to the request session.

    No ``LogService`` is injected — read paths never emit events.
    """
    return EffectiveAccessReadService(session)
