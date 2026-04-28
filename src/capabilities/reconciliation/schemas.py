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
    """Request body for POST /reconciliation/runs."""

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
    """Full run representation returned by GET /reconciliation/runs/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application_id: UUID
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
    operation: str
    natural_key_hash: str
    subject_id: UUID
    account_id: UUID | None = None
    resource_id: UUID
    action_id: int
    effect: str
    existing_fact_id: UUID | None = None
    source_artifact_id: UUID | None = None
    before_json: dict | None = None  # type: ignore[type-arg]
    after_json: dict | None = None  # type: ignore[type-arg]
    status: str
    reason: str | None = None
    created_at: datetime
    applied_at: datetime | None = None


class DeltaItemListResponse(BaseModel):
    """Cursor-paginated response for GET /reconciliation/runs/{id}/delta-items."""

    items: list[ReconciliationDeltaItemRead]
    next_cursor: str | None = None
