# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic v2 schemas for the deterministic report endpoint.

All models are frozen (immutable after construction). No severity enum is
imported from the SoD vocabulary — this slice is product-neutral.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict
from src.engines.access_analysis.analytics.schemas import FindingsSummary


class EvidenceSnippet(BaseModel):
    """Denormalized evidence context for a single finding."""

    model_config = ConfigDict(frozen=True)

    subject_external_id: str | None
    account_username: str | None
    application_id: uuid.UUID | None
    application_code: str | None


class TopFinding(BaseModel):
    """A high-severity open finding with its evidence snapshot."""

    model_config = ConfigDict(frozen=True)

    finding_id: int
    kind: str
    severity: str
    subject_id: uuid.UUID | None
    account_id: uuid.UUID | None
    detected_at: datetime
    evidence: EvidenceSnippet


class Recommendation(BaseModel):
    """Rule-based remediation recommendation derived from open findings."""

    model_config = ConfigDict(frozen=True)

    kind: str
    finding_kind: str
    severity_floor: str
    affected_finding_count: int
    text: str


class ExecutiveSummaryBlock(BaseModel):
    """One of the five fixed executive summary blocks."""

    model_config = ConfigDict(frozen=True)

    block_id: str
    title: str
    body: str
    metric: int | None


class DeterministicReport(BaseModel):
    """Product-neutral deterministic report envelope.

    GET /api/v0/reports/deterministic
    """

    model_config = ConfigDict(frozen=True)

    summary: FindingsSummary
    top_findings: list[TopFinding]
    recommendations: list[Recommendation]
    executive_summary: list[ExecutiveSummaryBlock]
    generated_at: datetime
