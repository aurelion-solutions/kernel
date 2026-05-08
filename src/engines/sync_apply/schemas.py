# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic schemas for the sync_apply capability slice."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.engines.sync_apply.models import SyncApplyRunMode, SyncApplyRunStatus


class SyncApplyApplyRequest(BaseModel):
    """Request body for POST /api/v0/reconciliation/runs/{id}/apply."""

    mode: SyncApplyRunMode = Field(..., description='Apply execution mode')
    item_ids: list[UUID] | None = Field(
        default=None,
        description='Specific delta item IDs to apply; only valid when mode=selected_items',
    )

    @model_validator(mode='after')
    def _validate_item_ids(self) -> SyncApplyApplyRequest:
        if self.mode == SyncApplyRunMode.selected_items:
            if not self.item_ids:
                raise ValueError('item_ids must be non-empty when mode=selected_items')
        else:
            if self.item_ids:
                raise ValueError('item_ids must be None or empty unless mode=selected_items')
        return self


class SyncApplyRunRead(BaseModel):
    """Sync/apply run representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reconciliation_run_id: UUID
    mode: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    applied_count: int = 0
    failed_count: int = 0
    error: str | None = None


class SyncApplyApplyResponse(BaseModel):
    """Response body for POST /api/v0/reconciliation/runs/{id}/apply."""

    apply_run_id: UUID
    status: SyncApplyRunStatus
    applied_count: int
    failed_count: int
    snapshot_ids: dict[str, int] = Field(default_factory=dict)


class SyncApplyResultRead(BaseModel):
    """Single sync/apply result row (reserved for Step 17)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sync_apply_run_id: UUID
    delta_item_id: UUID
    status: str
    fact_id: UUID | None = None
    snapshot_id: int | None = None
    error: str | None = None
    created_at: datetime
