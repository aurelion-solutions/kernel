# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PolicyService (Phase 6, Step 15)."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from src.capabilities.policy.loader import PolicyLoadError
from src.capabilities.policy.schemas import (
    AbstractState,
    Facts,
    OwnerFacts,
    SubjectFacts,
    TargetFacts,
    ThreatFacts,
)
from src.capabilities.policy.service import PolicyService
from src.platform.logs.service import noop_log_service

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

POLICIES_DIR = Path(__file__).resolve().parents[4] / 'resources' / 'policies'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: object) -> None:
    path.write_text(yaml.dump(data, default_flow_style=False), encoding='utf-8')


def _minimal_pack(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / 'lifecycle.yaml',
        {
            'rules': [
                {
                    'id': 'lc_active',
                    'kind': 'lifecycle',
                    'when': {'subject.kind': 'employee', 'subject.status': 'active'},
                    'then': {'abstract_state': 'enabled'},
                    'precedence': 10,
                }
            ]
        },
    )
    _write_yaml(
        tmp_path / 'risk.yaml',
        {
            'rules': [
                {
                    'id': 'risk_threat',
                    'kind': 'risk',
                    'when': {'threat.has_indicator': 'compromised'},
                    'then': {'risk_level': 'critical'},
                    'precedence': 100,
                }
            ]
        },
    )
    _write_yaml(
        tmp_path / 'mapping.yaml',
        [
            {
                'application': 'ad',
                'map': {
                    'enabled': {'concrete': 'active', 'actions': ['enable']},
                },
            }
        ],
    )


def _employee_facts(status: str, target_app: str | None = 'ad') -> Facts:
    target = TargetFacts(application=target_app) if target_app is not None else None
    return Facts(
        subject=SubjectFacts(id='emp-1', kind='employee', status=status),
        target=target,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 1. Init — default policies dir
# ---------------------------------------------------------------------------


def test_service_init_loads_rules() -> None:
    """PolicyService with default policies_dir loads non-empty lifecycle, risk and mapping."""
    svc = PolicyService(log_service=noop_log_service)
    pack = svc.get_rule_pack()
    assert len(pack.lifecycle) > 0
    assert len(pack.risk) > 0
    assert len(pack.mapping) > 0


# ---------------------------------------------------------------------------
# 2. Init — custom dir
# ---------------------------------------------------------------------------


def test_service_init_custom_dir(tmp_path: Path) -> None:
    """PolicyService pointing at a temp directory loads rules successfully."""
    _minimal_pack(tmp_path)
    svc = PolicyService(log_service=noop_log_service, policies_dir=tmp_path)
    pack = svc.get_rule_pack()
    assert len(pack.lifecycle) == 1
    assert len(pack.risk) == 1
    assert 'ad' in pack.mapping


# ---------------------------------------------------------------------------
# 3. Init — invalid dir raises
# ---------------------------------------------------------------------------


def test_service_init_invalid_dir_raises(tmp_path: Path) -> None:
    """PolicyService pointing at a nonexistent directory raises PolicyLoadError."""
    missing = tmp_path / 'nonexistent'
    with pytest.raises(PolicyLoadError):
        PolicyService(log_service=noop_log_service, policies_dir=missing)


# ---------------------------------------------------------------------------
# 4. Evaluate — terminated employee
# ---------------------------------------------------------------------------


def test_evaluate_policy_terminated_employee() -> None:
    """Terminated employee with AD target gets abstract_state=disabled and concrete_state set."""
    svc = PolicyService(log_service=noop_log_service, policies_dir=POLICIES_DIR)
    facts = _employee_facts(status='terminated', target_app='ad')
    decision = svc.evaluate_policy(facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state is not None


# ---------------------------------------------------------------------------
# 5. Evaluate — active employee
# ---------------------------------------------------------------------------


def test_evaluate_policy_active_employee() -> None:
    """Active employee with jira target gets abstract_state=enabled."""
    svc = PolicyService(log_service=noop_log_service, policies_dir=POLICIES_DIR)
    facts = _employee_facts(status='active', target_app='jira')
    decision = svc.evaluate_policy(facts)
    assert decision.abstract_state == AbstractState.enabled


# ---------------------------------------------------------------------------
# 6. Evaluate — NHI with terminated owner
# ---------------------------------------------------------------------------


def test_evaluate_policy_nhi_owner_terminated() -> None:
    """NHI with a terminated owner and github target gets abstract_state=disabled."""
    svc = PolicyService(log_service=noop_log_service, policies_dir=POLICIES_DIR)
    facts = Facts(
        subject=SubjectFacts(
            id='nhi-1',
            kind='nhi',
            status='active',
            owner=OwnerFacts(id='emp-99', status='terminated'),
        ),
        target=TargetFacts(application='github'),
        now=NOW,
    )
    decision = svc.evaluate_policy(facts)
    assert decision.abstract_state == AbstractState.disabled


# ---------------------------------------------------------------------------
# 7. Evaluate — IDP subject level (no target)
# ---------------------------------------------------------------------------


def test_evaluate_policy_idp_subject_level() -> None:
    """Terminated employee without a target (IDP mode) returns a decision with concrete_state=None."""
    svc = PolicyService(log_service=noop_log_service, policies_dir=POLICIES_DIR)
    facts = _employee_facts(status='terminated', target_app=None)
    decision = svc.evaluate_policy(facts)
    assert decision.abstract_state is not None
    assert decision.concrete_state is None


# ---------------------------------------------------------------------------
# 8. Evaluate — risk with threat indicators
# ---------------------------------------------------------------------------


def test_evaluate_policy_risk_with_threat() -> None:
    """Facts with credential_compromised threat indicator produce a decision with risk_level set."""
    svc = PolicyService(log_service=noop_log_service, policies_dir=POLICIES_DIR)
    facts = Facts(
        subject=SubjectFacts(id='emp-2', kind='employee', status='active'),
        target=TargetFacts(application='ad'),
        threat=ThreatFacts(active_indicators=['credential_compromised']),
        now=NOW,
    )
    decision = svc.evaluate_policy(facts)
    assert decision.risk_level is not None


# ---------------------------------------------------------------------------
# 9. Evaluate — emits log event
# ---------------------------------------------------------------------------


def test_evaluate_policy_emits_log_event() -> None:
    """evaluate_policy calls emit_safe once with event_type=policy.decision.made."""
    mock_log = MagicMock()
    svc = PolicyService(log_service=mock_log, policies_dir=POLICIES_DIR)
    facts = _employee_facts(status='active', target_app='ad')
    svc.evaluate_policy(facts)

    mock_log.emit_safe.assert_called_once()
    call_kwargs = mock_log.emit_safe.call_args

    # Positional args: event_type, level, message, component, payload
    args = call_kwargs.args
    assert args[0] == 'policy.decision.made'
    assert args[3] == 'policy-engine'

    payload = args[4]
    assert 'subject_id' in payload
    assert 'abstract_state' in payload


# ---------------------------------------------------------------------------
# 10. Evaluate — logger failure does not raise
# ---------------------------------------------------------------------------


def test_evaluate_policy_log_failure_does_not_raise() -> None:
    """If emit_safe raises internally, evaluate_policy still returns the Decision without error."""
    broken_log = MagicMock()
    broken_log.emit_safe.side_effect = RuntimeError('sink is broken')

    svc = PolicyService(log_service=broken_log, policies_dir=POLICIES_DIR)
    facts = _employee_facts(status='active', target_app='ad')

    # Service must guard against a broken sink even when emit_safe itself raises.
    try:
        decision = svc.evaluate_policy(facts)
    except RuntimeError:
        pytest.fail('evaluate_policy raised RuntimeError when emit_safe failed — must not propagate')
    else:
        assert decision is not None
