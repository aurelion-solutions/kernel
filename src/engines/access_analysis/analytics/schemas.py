# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Analytics Pydantic v2 response schemas.

Severity weights are named constants — NOT magic numbers in SQL.
MVP formula: risk_score = Σ(severity_weight × open_findings_count_in_severity).

NOTE: 100 × LOW findings can outscore 1 × CRITICAL — this is a documented
known limitation of the MVP non-canonical score. The canonical risk model is
a future phase concern.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Severity weights — named constants (per architecture-guardian requirement)
# ---------------------------------------------------------------------------

SEVERITY_WEIGHT_CRITICAL: int = 100
SEVERITY_WEIGHT_HIGH: int = 50
SEVERITY_WEIGHT_MEDIUM: int = 20
SEVERITY_WEIGHT_LOW: int = 5


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class TopRiskItem(BaseModel):
    """Risk entry for a single (subject_id, application_id) pair."""

    model_config = ConfigDict(frozen=True)

    subject_id: uuid.UUID
    application_id: uuid.UUID
    risk_score: int
    open_findings_count: int
    severity_breakdown: dict[str, int]


class RiskByApplicationItem(BaseModel):
    """Risk entry aggregated per application_id."""

    model_config = ConfigDict(frozen=True)

    application_id: uuid.UUID
    risk_score: int
    open_findings_count: int
    severity_breakdown: dict[str, int]


class TopRisksResponse(BaseModel):
    """Response for GET /analytics/top-risks."""

    model_config = ConfigDict(frozen=True)

    items: list[TopRiskItem]
    generated_at: datetime


class RiskByApplicationResponse(BaseModel):
    """Response for GET /analytics/risk-by-application."""

    model_config = ConfigDict(frozen=True)

    items: list[RiskByApplicationItem]
    generated_at: datetime


# ---------------------------------------------------------------------------
# Findings summary schemas (Phase 37) — PG-only, no risk score
# ---------------------------------------------------------------------------


class TopApplicationFindingCount(BaseModel):
    """Finding count for a single application_id."""

    model_config = ConfigDict(frozen=True)

    application_id: uuid.UUID
    finding_count: int


class TopSubjectFindingCount(BaseModel):
    """Finding count for a single subject_id."""

    model_config = ConfigDict(frozen=True)

    subject_id: uuid.UUID
    finding_count: int


class QuickWinFinding(BaseModel):
    """A single high/critical finding eligible for quick remediation."""

    model_config = ConfigDict(frozen=True)

    finding_id: int
    kind: str
    severity: str
    subject_id: uuid.UUID | None
    account_id: uuid.UUID | None
    detected_at: datetime


class FindingsSummary(BaseModel):
    """PG-only digest of current open finding state.

    GET /analytics/findings-summary
    """

    model_config = ConfigDict(frozen=True)

    total_findings: int
    findings_by_severity: dict[str, int]
    findings_by_kind: dict[str, int]
    critical_findings: int
    high_findings: int
    top_applications: list[TopApplicationFindingCount]
    top_subjects: list[TopSubjectFindingCount]
    quick_wins: list[QuickWinFinding]
    generated_at: datetime
