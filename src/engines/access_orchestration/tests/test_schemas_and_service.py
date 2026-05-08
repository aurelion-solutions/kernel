# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for access_orchestration schemas and service placeholder."""

from __future__ import annotations

import uuid

import pytest
from src.engines.access_orchestration.schemas import (
    AccessOperationType,
    AccessOrchestrationIntent,
    AccessOrchestrationResult,
    AccessOrchestrationSource,
)
from src.engines.access_orchestration.service import AccessOrchestrationService

# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


def test_operation_type_values() -> None:
    assert AccessOperationType.grant == 'grant'
    assert AccessOperationType.revoke == 'revoke'
    assert AccessOperationType.modify == 'modify'
    assert AccessOperationType.validate == 'validate'


def test_source_values() -> None:
    assert AccessOrchestrationSource.employee_request == 'employee_request'
    assert AccessOrchestrationSource.jml == 'jml'
    assert AccessOrchestrationSource.manager_action == 'manager_action'
    assert AccessOrchestrationSource.admin_action == 'admin_action'
    assert AccessOrchestrationSource.remediation == 'remediation'
    assert AccessOrchestrationSource.sod_mitigation == 'sod_mitigation'
    assert AccessOrchestrationSource.api == 'api'
    assert AccessOrchestrationSource.import_ == 'import'


# ---------------------------------------------------------------------------
# Intent construction
# ---------------------------------------------------------------------------


def test_intent_required_fields_only() -> None:
    intent = AccessOrchestrationIntent(
        operation_type=AccessOperationType.grant,
        source=AccessOrchestrationSource.employee_request,
    )
    assert intent.operation_type == AccessOperationType.grant
    assert intent.source == AccessOrchestrationSource.employee_request
    assert intent.subject_id is None
    assert intent.payload == {}


def test_intent_with_all_optional_fields() -> None:
    sid = uuid.uuid4()
    intent = AccessOrchestrationIntent(
        operation_type=AccessOperationType.revoke,
        source=AccessOrchestrationSource.jml,
        subject_id=sid,
        reason='employment terminated',
        payload={'ticket': 'JML-42'},
    )
    assert intent.subject_id == sid
    assert intent.reason == 'employment terminated'
    assert intent.payload['ticket'] == 'JML-42'


# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------


def test_result_defaults() -> None:
    result = AccessOrchestrationResult(accepted=True)
    assert result.accepted is True
    assert result.decision is None
    assert result.policy_output is None
    assert result.next_step is None
    assert result.signals == []
    assert result.payload == {}


# ---------------------------------------------------------------------------
# Service placeholder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_intent_accepts_all_intents() -> None:
    svc = AccessOrchestrationService()
    intent = AccessOrchestrationIntent(
        operation_type=AccessOperationType.grant,
        source=AccessOrchestrationSource.api,
    )
    result = await svc.handle_intent(intent)
    assert result.accepted is True


@pytest.mark.asyncio
async def test_handle_intent_returns_policy_assessment_next_step() -> None:
    svc = AccessOrchestrationService()
    intent = AccessOrchestrationIntent(
        operation_type=AccessOperationType.validate,
        source=AccessOrchestrationSource.sod_mitigation,
    )
    result = await svc.handle_intent(intent)
    assert result.next_step == 'policy_assessment_required'


@pytest.mark.asyncio
async def test_handle_intent_is_deterministic() -> None:
    svc = AccessOrchestrationService()
    intent = AccessOrchestrationIntent(
        operation_type=AccessOperationType.modify,
        source=AccessOrchestrationSource.remediation,
    )
    r1 = await svc.handle_intent(intent)
    r2 = await svc.handle_intent(intent)
    assert r1 == r2
