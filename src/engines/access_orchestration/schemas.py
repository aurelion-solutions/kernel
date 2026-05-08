# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic contracts for access orchestration intents and results."""

from __future__ import annotations

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


class AccessOrchestrationSource(StrEnum):
    """What triggered this orchestration intent."""

    employee_request = 'employee_request'
    jml = 'jml'
    manager_action = 'manager_action'
    admin_action = 'admin_action'
    remediation = 'remediation'
    sod_mitigation = 'sod_mitigation'
    api = 'api'
    import_ = 'import'


class AccessOrchestrationIntent(BaseModel):
    """An explicit intent to change or validate access state.

    Carries both the operation type and the originating source so downstream
    engines can apply source-specific policy or approval logic.
    """

    operation_type: AccessOperationType
    source: AccessOrchestrationSource
    subject_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    action_id: uuid.UUID | None = None
    requested_by_subject_id: uuid.UUID | None = None
    reason: str | None = None
    payload: dict[str, Any] = {}


class AccessOrchestrationResult(BaseModel):
    """Structured result returned after orchestrating an intent.

    accepted: whether the orchestration layer accepted the intent for processing.
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
