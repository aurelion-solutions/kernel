# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic contracts for access plan intents and results."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

from pydantic import BaseModel
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput
from src.engines.policy_assessment.schemas import Decision


class AccessOperationType(StrEnum):
    """The kind of access operation being requested."""

    grant = 'grant'
    revoke = 'revoke'
    modify = 'modify'
    validate = 'validate'


class AccessPlanSource(StrEnum):
    """What triggered this access plan intent."""

    employee_request = 'employee_request'
    jml = 'jml'
    manager_action = 'manager_action'
    admin_action = 'admin_action'
    remediation = 'remediation'
    sod_mitigation = 'sod_mitigation'
    api = 'api'
    import_ = 'import'


class AccessPlanIntent(BaseModel):
    """An explicit intent to change or validate access state.

    Carries both the operation type and the originating source so downstream
    engines can apply source-specific policy or approval logic.
    """

    operation_type: AccessOperationType
    source: AccessPlanSource
    subject_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    action_id: uuid.UUID | None = None
    requested_by_subject_id: uuid.UUID | None = None
    reason: str | None = None
    payload: dict[str, Any] = {}


class PlanItemRead(BaseModel):
    """Flat representation of a PlanItem joined with its execution state and parent plan."""

    id: uuid.UUID
    plan_id: uuid.UUID
    plan_status: str
    subject_ref: str
    subject_type: str
    kind: str
    application: str
    account_ref: str | None
    target_descriptor: dict[str, Any]
    initiatives: list[dict[str, Any]]
    initiative_refs: list[Any]
    policy_rule_refs: list[str]
    decision_snapshot: dict[str, Any]
    execution_status: str
    failure_reason: str | None
    last_verified_at: datetime | None
    last_error: str | None
    created_at: datetime
    # display fields — nullable; populated by list endpoint batch lookup
    subject_display: str | None = None
    application_code: str | None = None
    application_name: str | None = None
    target_display: str | None = None
    change_summary: str | None = None


class PlanItemListResponse(BaseModel):
    """Response for the flat plan-items list endpoint."""

    items: list[PlanItemRead]
    total: int


class PlanItemCountResponse(BaseModel):
    """Response for the plan-items count endpoint."""

    count: int


class AccessPlanResult(BaseModel):
    """Structured result returned after processing an access plan intent.

    accepted: whether the access_plan engine accepted the intent for processing.
    decision: policy Decision, if policy_assessment was consulted.
    policy_output: full PolicyAssessmentOutput, if available.
    next_step: identifier of the next action required (e.g. 'policy_assessment_required').
    signals: short tags summarising why this result was produced.
    payload: engine-specific extras for debugging/tracing.
    """

    accepted: bool
    decision: Decision | None = None
    policy_output: PolicyAssessmentOutput | None = None
    next_step: str | None = None
    signals: list[str] = []
    payload: dict[str, Any] = {}
