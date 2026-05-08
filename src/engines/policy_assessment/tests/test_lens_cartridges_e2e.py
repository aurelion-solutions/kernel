# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end tests: real Lens cartridge YAML files evaluated through
PolicyCartridgeAssessmentService with a live dispatcher.

The dispatcher is backed by a mock PolicyService (never called, since all
cartridges carry a 'condition' key and are routed to evaluate_deterministic_cartridge).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.engines.policy_assessment.cartridge_service import PolicyCartridgeAssessmentService
from src.engines.policy_assessment.dispatcher import PolicyAssessmentDispatcher
from src.engines.policy_assessment.schemas import RiskLevel
from src.engines.policy_assessment.service import PolicyService

LENS_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / 'cartridges' / 'lens'


def _svc() -> PolicyCartridgeAssessmentService:
    dispatcher = PolicyAssessmentDispatcher(policy_service=MagicMock(spec=PolicyService))
    return PolicyCartridgeAssessmentService(dispatcher=dispatcher)


# ---------------------------------------------------------------------------
# orphaned_access
# ---------------------------------------------------------------------------

_ORPHANED_PATH = LENS_DIR / 'access_risk' / 'orphaned_access.yaml'


def test_orphaned_access_match() -> None:
    result = _svc().evaluate_file(_ORPHANED_PATH, {'subject_not_found': True})
    assert result.matched is True
    assert result.decision is not None
    assert result.decision.risk_level == RiskLevel.high
    assert 'flag_for_review' in result.decision.actions


def test_orphaned_access_no_match_subject_found() -> None:
    result = _svc().evaluate_file(_ORPHANED_PATH, {'subject_not_found': False})
    assert result.matched is False
    assert result.decision is None


def test_orphaned_access_no_match_missing_fact() -> None:
    result = _svc().evaluate_file(_ORPHANED_PATH, {})
    assert result.matched is False


def test_orphaned_access_payload_contains_id() -> None:
    result = _svc().evaluate_file(_ORPHANED_PATH, {'subject_not_found': True})
    assert result.payload.get('id') == 'lens.access_risk.orphaned_access'
    assert result.payload.get('rule_id') == 'lens.access_risk.orphaned_access'


# ---------------------------------------------------------------------------
# terminated_subject_access
# ---------------------------------------------------------------------------

_TERMINATED_PATH = LENS_DIR / 'lifecycle' / 'terminated_subject_access.yaml'


def test_terminated_subject_access_match_employee() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {'subject': {'status': 'terminated'}})
    assert result.matched is True
    assert result.decision is not None
    assert result.decision.risk_level == RiskLevel.critical
    assert 'revoke' in result.decision.actions


def test_terminated_subject_access_match_nhi_expired() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {'subject': {'status': 'expired'}})
    assert result.matched is True


def test_terminated_subject_access_match_nhi_locked() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {'subject': {'status': 'locked'}})
    assert result.matched is True


def test_terminated_subject_access_match_customer_banned() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {'subject': {'status': 'banned'}})
    assert result.matched is True


def test_terminated_subject_access_match_customer_deletion_requested() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {'subject': {'status': 'deletion_requested'}})
    assert result.matched is True


def test_terminated_subject_access_no_match_active_subject() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {'subject': {'status': 'active'}})
    assert result.matched is False
    assert result.decision is None


def test_terminated_subject_access_no_match_missing_status() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {})
    assert result.matched is False


def test_terminated_subject_access_payload_contains_id() -> None:
    result = _svc().evaluate_file(_TERMINATED_PATH, {'subject': {'status': 'terminated'}})
    assert result.payload.get('id') == 'lens.lifecycle.terminated_subject_access'


# ---------------------------------------------------------------------------
# unused_access
# Note: original condition used cross-fact comparison
# (days_since_last_use > config.inactivity_threshold_days) which requires
# context-variable thresholds not yet in the DSL. Condition updated to
# greater_than with literal threshold 90 until cross-fact comparison lands.
# ---------------------------------------------------------------------------

_UNUSED_PATH = LENS_DIR / 'access_risk' / 'unused_access.yaml'


def test_unused_access_match_over_threshold() -> None:
    result = _svc().evaluate_file(_UNUSED_PATH, {'days_since_last_use': 120})
    assert result.matched is True
    assert result.decision is not None
    assert result.decision.risk_level == RiskLevel.medium


def test_unused_access_no_match_under_threshold() -> None:
    result = _svc().evaluate_file(_UNUSED_PATH, {'days_since_last_use': 30})
    assert result.matched is False


def test_unused_access_no_match_at_threshold() -> None:
    result = _svc().evaluate_file(_UNUSED_PATH, {'days_since_last_use': 90})
    assert result.matched is False


# ---------------------------------------------------------------------------
# privileged_access
# ---------------------------------------------------------------------------

_PRIVILEGED_PATH = LENS_DIR / 'access_risk' / 'privileged_access.yaml'


def test_privileged_access_match_account_is_privileged() -> None:
    ctx = {
        'account_is_privileged': True,
        'action': 'read',
        'resource_privilege_level': 'read',
        'environment': 'production',
        'data_sensitivity': 'pii',
    }
    result = _svc().evaluate_file(_PRIVILEGED_PATH, ctx)
    assert result.matched is True
    assert result.decision is not None
    assert result.decision.risk_level == RiskLevel.high


def test_privileged_access_match_admin_action_on_admin_resource() -> None:
    ctx = {
        'account_is_privileged': False,
        'action': 'administer',
        'resource_privilege_level': 'admin',
        'environment': 'production',
        'data_sensitivity': 'public',
    }
    result = _svc().evaluate_file(_PRIVILEGED_PATH, ctx)
    assert result.matched is True
    assert result.decision is not None
    assert result.decision.risk_level == RiskLevel.high


def test_privileged_access_no_match_admin_action_on_non_admin_resource() -> None:
    ctx = {
        'account_is_privileged': False,
        'action': 'administer',
        'resource_privilege_level': 'write',
        'environment': 'production',
        'data_sensitivity': 'public',
    }
    result = _svc().evaluate_file(_PRIVILEGED_PATH, ctx)
    assert result.matched is False


def test_privileged_access_no_match_read_action_unprivileged_account() -> None:
    ctx = {
        'account_is_privileged': False,
        'action': 'read',
        'resource_privilege_level': 'read',
        'environment': 'production',
        'data_sensitivity': 'public',
    }
    result = _svc().evaluate_file(_PRIVILEGED_PATH, ctx)
    assert result.matched is False


def test_privileged_access_no_match_when_resource_privilege_level_missing() -> None:
    """Missing resource_privilege_level → admin equality fails, account flag still controls."""
    ctx = {
        'account_is_privileged': False,
        'action': 'administer',
        # resource_privilege_level intentionally absent
    }
    result = _svc().evaluate_file(_PRIVILEGED_PATH, ctx)
    assert result.matched is False


def test_privileged_access_payload_contains_id() -> None:
    ctx = {'account_is_privileged': True, 'action': 'read', 'resource_privilege_level': 'read'}
    result = _svc().evaluate_file(_PRIVILEGED_PATH, ctx)
    assert result.payload.get('id') == 'lens.access_risk.privileged_access'


# ---------------------------------------------------------------------------
# SoD intentionally omitted: DB-backed, no runnable file cartridge.
# See cartridges/templates/sod/toxic_combination.template.yaml for the
# documentation-only SoD template.
# ---------------------------------------------------------------------------
