# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Finding Pydantic v2 schemas.

No FindingCreate — findings are written by the engine in later steps.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict
from src.capabilities.access_analysis.findings.models import FindingKind, FindingStatus
from src.capabilities.access_analysis.sod_rules.models import SodSeverity


class FindingStatusPatch(BaseModel):
    """Body for PATCH /findings/{id}/status."""

    model_config = ConfigDict(extra='forbid')

    status: FindingStatus
    status_reason: str | None = None
    active_mitigation_id: int | None = None


class FindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scan_run_id: int
    kind: FindingKind
    subject_id: uuid.UUID | None
    account_id: uuid.UUID | None
    rule_id: int | None
    scope_key_id: int | None
    scope_value: str | None
    severity: SodSeverity
    status: FindingStatus
    matched_capability_grant_ids: list
    matched_effective_grant_ids: list
    matched_access_fact_ids: list
    evidence_hash: str
    active_mitigation_id: int | None
    proposed_mitigation_id: int | None
    detected_at: datetime
    evaluated_at: datetime
    status_changed_at: datetime | None
    status_reason: str | None
