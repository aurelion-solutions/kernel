# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Request/response Pydantic schemas for detector routes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.engines.policy_assessment.policy_types.access_risk.evaluator import OrphanFinding, UnusedFinding
from src.engines.policy_assessment.policy_types.lifecycle.evaluator import TerminatedFinding
from src.inventory.policy.sod_rules.models import SodSeverity
from src.inventory.subjects.models import SubjectKind


class DetectOrphansRequest(BaseModel):
    """Request body for POST /access-analysis/detect-orphans."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID | None = None
    limit: int = Field(default=1000, ge=1, le=5000)


class OrphanFindingResponse(BaseModel):
    """Pydantic mirror of OrphanFinding dataclass for JSON serialization.

    UUIDs are serialized as strings for JSON-friendly output.
    """

    model_config = ConfigDict(from_attributes=False)

    account_id: str
    application_id: str
    username: str
    severity: SodSeverity
    last_known_owner_subject_id: str | None
    detected_at: datetime

    @classmethod
    def from_orphan_finding(cls, f: OrphanFinding) -> OrphanFindingResponse:
        """Build from an OrphanFinding dataclass instance."""
        return cls(
            account_id=str(f.account_id),
            application_id=str(f.application_id),
            username=f.username,
            severity=f.severity,
            last_known_owner_subject_id=str(f.last_known_owner_subject_id)
            if f.last_known_owner_subject_id is not None
            else None,
            detected_at=f.detected_at,
        )


class DetectTerminatedRequest(BaseModel):
    """Request body for POST /access-analysis/detect-terminated."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID | None = None
    limit: int = Field(default=1000, ge=1, le=5000)


class TerminatedFindingResponse(BaseModel):
    """Pydantic mirror of TerminatedFinding dataclass for JSON serialization.

    UUIDs are serialized as strings for JSON-friendly output.
    """

    model_config = ConfigDict(from_attributes=False)

    account_id: str
    application_id: str
    username: str
    subject_id: str
    subject_kind: SubjectKind
    subject_status: str
    subject_external_id: str
    severity: SodSeverity
    detected_at: datetime

    @classmethod
    def from_terminated_finding(cls, f: TerminatedFinding) -> TerminatedFindingResponse:
        """Build from a TerminatedFinding dataclass instance."""
        return cls(
            account_id=str(f.account_id),
            application_id=str(f.application_id),
            username=f.username,
            subject_id=str(f.subject_id),
            subject_kind=f.subject_kind,
            subject_status=f.subject_status,
            subject_external_id=f.subject_external_id,
            severity=f.severity,
            detected_at=f.detected_at,
        )


class DetectUnusedRequest(BaseModel):
    """Request body for POST /access-analysis/detect-unused."""

    model_config = ConfigDict(extra='forbid')

    application_id: UUID | None = None
    threshold_days: int = Field(default=90, ge=1, le=3650)
    limit: int = Field(default=1000, ge=1, le=5000)


class UnusedFindingResponse(BaseModel):
    """Pydantic mirror of UnusedFinding dataclass for JSON serialization.

    UUIDs are serialized as strings for JSON-friendly output.
    """

    model_config = ConfigDict(from_attributes=False)

    access_fact_id: str
    subject_id: str
    account_id: str | None
    resource_id: str
    application_id: str
    last_seen: datetime | None
    valid_from: datetime
    unused_for_days: int
    severity: SodSeverity
    detected_at: datetime

    @classmethod
    def from_unused_finding(cls, f: UnusedFinding) -> UnusedFindingResponse:
        """Build from an UnusedFinding dataclass instance."""
        return cls(
            access_fact_id=str(f.access_fact_id),
            subject_id=str(f.subject_id),
            account_id=str(f.account_id) if f.account_id is not None else None,
            resource_id=str(f.resource_id),
            application_id=str(f.application_id),
            last_seen=f.last_seen,
            valid_from=f.valid_from,
            unused_for_days=f.unused_for_days,
            severity=f.severity,
            detected_at=f.detected_at,
        )
