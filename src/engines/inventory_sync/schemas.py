# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic schemas for the sync_apply capability slice."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.engines.inventory_sync.models import SyncApplyRunMode, SyncApplyRunStatus

# ---------------------------------------------------------------------------
# Phase 19 Step F2: shared FactDescriptor + SingleFactSyncOp
# ---------------------------------------------------------------------------


class SingleFactSyncOp(str, Enum):
    """Operation type for a single-fact sync call.

    ``grant`` — positive event (access granted / granted-state confirmed).
    ``revoke`` — negative event (access revoked).

    lake-based effective access resolves the latest event per (subject, target),
    so functional idempotency is guaranteed: even if a duplicate slips through
    between the preflight scan and the append, the effective state stays correct.
    """

    grant = 'grant'
    revoke = 'revoke'


class FactDescriptor(BaseModel):
    """Shared descriptor for a single access fact.

    Used in:
    - ``inventory_sync.sync_single_fact(descriptor, op, event_key)``
    - ``connector.verify_fact(descriptor, expected_state)``

    ``kind`` identifies the fact category (e.g. ``role_grant``, ``group_membership``).
    ``application`` is the connector application identifier.
    ``target_descriptor`` holds connector-specific target details (e.g. role id,
    group id, entitlement key) as a freeform dict.
    ``account_ref`` is optional — some facts are subject-scoped, not account-scoped.
    ``initiative_refs`` is optional — list of Initiative UUIDs associated with this fact.
    """

    kind: str = Field(min_length=1, max_length=128, description='Fact kind (e.g. role_grant).')
    application: str = Field(min_length=1, max_length=255, description='Connector application identifier.')
    target_descriptor: dict[str, object] = Field(
        default_factory=dict,
        description='Connector-specific target details (role id, group id, etc.).',
    )
    account_ref: str | None = Field(
        default=None,
        description='Optional account reference; None for subject-scoped facts.',
    )
    initiative_refs: list[UUID] | None = Field(
        default=None,
        description='Optional list of Initiative UUIDs associated with this fact.',
    )


class SyncApplyApplyRequest(BaseModel):
    """Request body for POST /api/v0/inventory-reconciles/runs/{id}/apply."""

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
    """Response body for POST /api/v0/inventory-reconciles/runs/{id}/apply."""

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
