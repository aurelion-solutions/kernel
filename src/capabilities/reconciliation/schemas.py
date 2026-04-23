# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation request / response schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ReconciliationRunRequest(BaseModel):
    """Request body for POST /reconciliation/runs."""

    application_id: UUID = Field(..., description='Application to reconcile')


class ReconciliationRunSummary(BaseModel):
    """Result summary returned after a reconciliation run."""

    application_id: UUID
    started_at: datetime
    finished_at: datetime
    artifacts_ingested: int = Field(ge=0)
    facts_created: int = Field(ge=0)
    facts_updated: int = Field(ge=0)
    facts_revoked: int = Field(ge=0)
    artifacts_unhandled: int = Field(ge=0)
    facts_errored: int = Field(ge=0, default=0)
