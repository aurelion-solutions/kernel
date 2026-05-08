# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun Pydantic v2 schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict
from src.inventory.assessment.scan_runs.models import ScanRunStatus, ScanRunTrigger


class ScanRunCreate(BaseModel):
    """Body for POST /scan-runs. Creates a pending run."""

    model_config = ConfigDict(extra='forbid')

    triggered_by: ScanRunTrigger
    scope_subject_id: uuid.UUID | None = None
    scope_application_id: uuid.UUID | None = None
    created_by: str | None = None


class ScanRunStatusPatch(BaseModel):
    """Body for PATCH /scan-runs/{id}/status."""

    model_config = ConfigDict(extra='forbid')

    status: ScanRunStatus
    error_message: str | None = None


class ScanRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: ScanRunStatus
    triggered_by: ScanRunTrigger
    started_at: datetime | None
    completed_at: datetime | None
    scope_subject_id: uuid.UUID | None
    scope_application_id: uuid.UUID | None
    findings_total: int
    findings_by_severity: dict
    findings_created_count: int
    findings_reused_count: int
    error_message: str | None
    created_at: datetime
    created_by: str | None
