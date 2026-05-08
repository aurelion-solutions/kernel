# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityGrant read-only service.

CapabilityGrantReadService:
- list_grants / get_grant: pass-through read queries.

Forbidden: emit_safe, emit_log, session.flush, session.commit, session.rollback.

Write path (projection) lives in:
  src/engines/access_analysis/services/capability_projection.py
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_model.capability_grants.models import CapabilityGrant
from src.inventory.access_model.capability_grants.repository import (
    get_capability_grant,
    list_capability_grants,
)

# ---------------------------------------------------------------------------
# Read service
# ---------------------------------------------------------------------------


class CapabilityGrantReadService:
    """Read-only query driver for CapabilityGrant.

    No events, no flush, no session mutation.

    Forbidden: emit_safe, emit_log, session.flush(), session.commit(), session.rollback().
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_grants(
        self,
        *,
        subject_id: UUID | None = None,
        capability_id: int | None = None,
        scope_key_id: int | None = None,
        scope_value: str | None = None,
        application_id: UUID | None = None,
        source_effective_grant_id: UUID | None = None,
        source_capability_mapping_id: int | None = None,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
        now: datetime | None = None,
    ) -> list[CapabilityGrant]:
        """Pass-through to list_capability_grants."""
        return await list_capability_grants(
            self._session,
            subject_id=subject_id,
            capability_id=capability_id,
            scope_key_id=scope_key_id,
            scope_value=scope_value,
            application_id=application_id,
            source_effective_grant_id=source_effective_grant_id,
            source_capability_mapping_id=source_capability_mapping_id,
            active_only=active_only,
            limit=limit,
            offset=offset,
            now=now,
        )

    async def get_grant(self, grant_id: int) -> CapabilityGrant | None:
        """Pass-through to get_capability_grant."""
        return await get_capability_grant(self._session, grant_id)
