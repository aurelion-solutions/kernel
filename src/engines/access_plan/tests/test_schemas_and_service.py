# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for access_plan schemas and service placeholder."""

from __future__ import annotations

import uuid

from src.engines.access_plan.schemas import (
    AccessOperationType,
    AccessPlanIntent,
    AccessPlanResult,
    AccessPlanSource,
)

# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------


def test_operation_type_values() -> None:
    assert AccessOperationType.grant == 'grant'
    assert AccessOperationType.revoke == 'revoke'
    assert AccessOperationType.modify == 'modify'
    assert AccessOperationType.validate == 'validate'


def test_source_values() -> None:
    assert AccessPlanSource.employee_request == 'employee_request'
    assert AccessPlanSource.jml == 'jml'
    assert AccessPlanSource.manager_action == 'manager_action'
    assert AccessPlanSource.admin_action == 'admin_action'
    assert AccessPlanSource.remediation == 'remediation'
    assert AccessPlanSource.sod_mitigation == 'sod_mitigation'
    assert AccessPlanSource.api == 'api'
    assert AccessPlanSource.import_ == 'import'


# ---------------------------------------------------------------------------
# Intent construction
# ---------------------------------------------------------------------------


def test_intent_required_fields_only() -> None:
    intent = AccessPlanIntent(
        operation_type=AccessOperationType.grant,
        source=AccessPlanSource.employee_request,
    )
    assert intent.operation_type == AccessOperationType.grant
    assert intent.source == AccessPlanSource.employee_request
    assert intent.subject_id is None
    assert intent.payload == {}


def test_intent_with_all_optional_fields() -> None:
    sid = uuid.uuid4()
    intent = AccessPlanIntent(
        operation_type=AccessOperationType.revoke,
        source=AccessPlanSource.jml,
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
    result = AccessPlanResult(accepted=True)
    assert result.accepted is True
    assert result.decision is None
    assert result.policy_output is None
    assert result.next_step is None
    assert result.signals == []
    assert result.payload == {}


# Note: placeholder service tests (test_handle_intent_*) removed in D2 —
# AccessPlanService.handle_intent replaced by create_plan (D2 implementation).
# See test_service_d2.py for the new service tests.
