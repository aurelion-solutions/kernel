# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation request / response schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReconciliationRunMode(StrEnum):
    """Supported reconciliation execution modes."""

    review = 'review'
    dry_run = 'dry_run'
    auto_apply = 'auto_apply'


class ReconciliationRunRequest(BaseModel):
    """Request body for POST /inventory-reconciles/runs."""

    application_id: UUID = Field(..., description='Application to reconcile')
    mode: ReconciliationRunMode = Field(
        default=ReconciliationRunMode.review,
        description='Execution mode: review (default), dry_run, or auto_apply',
    )


class ReconciliationRunSummary(BaseModel):
    """Result summary returned after a reconciliation run.

    Kept for backward compatibility; routes now return ReconciliationRunRead.
    """

    run_id: UUID | None = None
    application_id: UUID
    started_at: datetime
    finished_at: datetime
    artifacts_ingested: int = Field(ge=0)
    facts_created: int = Field(ge=0)
    facts_updated: int = Field(ge=0)
    facts_revoked: int = Field(ge=0)
    artifacts_unhandled: int = Field(ge=0)
    facts_errored: int = Field(ge=0, default=0)
    unchanged_count: int = Field(ge=0, default=0)
    observed_snapshot_id: int | None = None
    current_snapshot_id: int | None = None


class ReconciliationRunRead(BaseModel):
    """Full run representation returned by GET /inventory-reconciles/runs/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application_id: UUID | None = None
    entity_type: str | None = 'access_fact'
    status: str
    observed_snapshot_id: int | None = None
    current_snapshot_id: int | None = None
    observed_batch_id: UUID | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_count: int = 0
    updated_count: int = 0
    revoked_count: int = 0
    unchanged_count: int = 0
    error: str | None = None


class ReconciliationDeltaItemRead(BaseModel):
    """Single delta item representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reconciliation_run_id: UUID
    # present in cross-run responses; None for per-run responses
    application_id: UUID | None = None
    entity_type: str | None = 'access_fact'
    operation: str
    # access_fact-specific (None for master data rows)
    natural_key_hash: str | None = None
    subject_id: UUID | None = None
    account_id: UUID | None = None
    resource_id: UUID | None = None
    action_id: int | None = None
    effect: str | None = None
    # master data-specific (None for access_fact rows)
    entity_id: UUID | None = None
    existing_fact_id: UUID | None = None
    source_artifact_id: UUID | None = None
    before_json: dict | None = None  # type: ignore[type-arg]
    after_json: dict | None = None  # type: ignore[type-arg]
    status: str
    reason: str | None = None
    created_at: datetime
    applied_at: datetime | None = None
    # display fields — nullable; populated by enriched list endpoint
    subject_display: str | None = None
    account_display: str | None = None
    resource_display: str | None = None
    application_code: str | None = None
    application_name: str | None = None
    change_summary: str | None = None


class DeltaItemListResponse(BaseModel):
    """Cursor-paginated response for GET /inventory-reconciles/runs/{id}/delta-items."""

    items: list[ReconciliationDeltaItemRead]
    next_cursor: str | None = None


class DeltaItemCountResponse(BaseModel):
    """Response for GET /inventory-reconciles/delta-items/count."""

    count: int


# ---------------------------------------------------------------------------
# Master data reconciliation schemas
# ---------------------------------------------------------------------------


class MasterDataReconciliationRequest(BaseModel):
    """Request body for POST /inventory-reconciles/master-data/runs."""

    entity_type: str = Field(..., description="'person', 'org_unit', or 'employee'")


class MasterDataApplyRequest(BaseModel):
    """Request body for POST /inventory-reconciles/master-data/runs/{id}/apply."""

    entity_type: str = Field(..., description="'person', 'org_unit', or 'employee'")


class MasterDataRunRead(BaseModel):
    """Response for master data reconciliation endpoints."""

    run_id: UUID
    entity_type: str
    status: str
    created_count: int = 0
    updated_count: int = 0
    revoked_count: int = 0
    unchanged_count: int = 0
    applied_count: int = 0
    failed_count: int = 0
    ignored_count: int = 0
