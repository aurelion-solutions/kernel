# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Request/response Pydantic schemas for the SoD Evaluator route."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator
from src.capabilities.access_analysis.sod_rules.models import SodRuleScope, SodSeverity


class SodEvaluateRequest(BaseModel):
    """Request body for POST /sod/evaluate."""

    model_config = ConfigDict(extra='forbid')

    subject_id: UUID
    at: datetime | None = None


class CapabilityGrantOverride(BaseModel):
    """One synthetic capability grant for what-if analysis.

    The caller supplies only the fields needed for bucketing and slug intersection.
    Synthetic fields (id, subject_id, capability_slug, source_*) are filled in by the
    service layer — they are NOT exposed to the API caller.

    scope_value normalization rules (validated here, not in the service):
    - must be stripped of surrounding whitespace
    - must be lowercase
    - max 255 characters
    - None is only valid when scope_key_id resolves to a GLOBAL key (checked server-side)
    """

    model_config = ConfigDict(extra='forbid')

    capability_id: int
    scope_key_id: int
    scope_value: str | None = None
    application_id: UUID

    @field_validator('scope_value')
    @classmethod
    def _validate_scope_value_normalization(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v != v.strip() or v != v.lower() or len(v) > 255:
            from src.capabilities.access_analysis.evaluators.exceptions import WhatIfScopeValueInvalidError

            raise WhatIfScopeValueInvalidError(v)
        return v


class SodWhatIfRequest(BaseModel):
    """Request body for POST /sod/what-if."""

    model_config = ConfigDict(extra='forbid')

    subject_id: UUID
    at: datetime | None = None
    capability_overrides: list[CapabilityGrantOverride] = []


class SodViolationResponse(BaseModel):
    """Pydantic mirror of Violation dataclass for JSON serialization.

    matched_effective_grant_ids: serialized as list[str] (UUIDs → strings).
    """

    model_config = ConfigDict(from_attributes=False)

    rule_id: int
    rule_code: str
    severity: SodSeverity
    scope_mode: SodRuleScope
    scope_key_id: int | None
    scope_value: str | None
    matched_condition_ids: list[int]
    matched_capability_slugs: list[str]
    matched_capability_grant_ids: list[int]
    matched_effective_grant_ids: list[str]
    evidence_hash: str
    is_mitigated: bool
    active_mitigation_id: int | None
    proposed_mitigation_id: int | None
    evaluated_at: datetime

    @classmethod
    def from_violation(
        cls,
        v: object,
    ) -> SodViolationResponse:
        """Build from a Violation dataclass instance."""
        from src.capabilities.access_analysis.evaluators.sod import Violation

        assert isinstance(v, Violation)
        return cls(
            rule_id=v.rule_id,
            rule_code=v.rule_code,
            severity=v.severity,
            scope_mode=v.scope_mode,
            scope_key_id=v.scope_key_id,
            scope_value=v.scope_value,
            matched_condition_ids=v.matched_condition_ids,
            matched_capability_slugs=v.matched_capability_slugs,
            matched_capability_grant_ids=v.matched_capability_grant_ids,
            matched_effective_grant_ids=[str(eid) for eid in v.matched_effective_grant_ids],
            evidence_hash=v.evidence_hash,
            is_mitigated=v.is_mitigated,
            active_mitigation_id=v.active_mitigation_id,
            proposed_mitigation_id=v.proposed_mitigation_id,
            evaluated_at=v.evaluated_at,
        )
