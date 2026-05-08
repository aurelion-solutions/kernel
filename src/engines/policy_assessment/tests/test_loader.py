# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP YAML loader (Phase 6, Step 13)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from src.engines.policy_assessment.loader import PolicyLoadError, load_rules_from_yaml
from src.engines.policy_assessment.schemas import AbstractState, Facts, RulePack, SubjectFacts, TargetFacts
from src.engines.policy_assessment.strategies.deterministic.evaluator import evaluate

# Path to the canonical YAML fixtures shipped with the kernel.
POLICIES_DIR = Path(__file__).parent.parent.parent.parent.parent / 'resources' / 'policies'

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def canonical_pack() -> RulePack:
    """Load the canonical RulePack once per test session."""
    return load_rules_from_yaml(POLICIES_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: object) -> None:
    path.write_text(yaml.dump(data, default_flow_style=False), encoding='utf-8')


def _minimal_lifecycle(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / 'lifecycle.yaml',
        {
            'rules': [
                {
                    'id': 'lc_1',
                    'kind': 'lifecycle',
                    'when': {'subject.kind': 'employee'},
                    'then': {'abstract_state': 'enabled'},
                    'precedence': 10,
                }
            ]
        },
    )


def _minimal_risk(tmp_path: Path) -> None:
    _write_yaml(
        tmp_path / 'risk.yaml',
        {
            'rules': [
                {
                    'id': 'risk_1',
                    'kind': 'risk',
                    'when': {'threat.has_indicator': 'x'},
                    'then': {'risk_level': 'high'},
                    'precedence': 100,
                }
            ]
        },
    )


def _minimal_mapping(tmp_path: Path) -> None:
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


def _minimal_pack(tmp_path: Path) -> None:
    _minimal_lifecycle(tmp_path)
    _minimal_risk(tmp_path)
    _minimal_mapping(tmp_path)


# ---------------------------------------------------------------------------
# 1. Structure and format validation — canonical files
# ---------------------------------------------------------------------------


def test_load_lifecycle_rules_count(canonical_pack: RulePack) -> None:
    """Canonical lifecycle.yaml must contain exactly 30 rules."""
    assert len(canonical_pack.lifecycle) == 30


def test_load_risk_rules_count(canonical_pack: RulePack) -> None:
    """Canonical risk.yaml must contain exactly 17 rules."""
    assert len(canonical_pack.risk) == 17


def test_load_mapping_applications(canonical_pack: RulePack) -> None:
    """Canonical mapping.yaml must have exactly 5 application keys."""
    assert set(canonical_pack.mapping.keys()) == {'ad', 'jira', 'github', 'stripe_billing', 'customer_portal'}


def test_all_rule_ids_unique(canonical_pack: RulePack) -> None:
    """All rule IDs across lifecycle + risk must be unique."""
    all_ids = [r.id for r in canonical_pack.lifecycle] + [r.id for r in canonical_pack.risk]
    assert len(all_ids) == len(set(all_ids))


def test_all_rules_have_required_fields(canonical_pack: RulePack) -> None:
    """Every rule must have id (str), kind (str), when (dict), then (dict), precedence (int)."""
    for rule in canonical_pack.lifecycle + canonical_pack.risk:
        assert isinstance(rule.id, str) and rule.id
        assert isinstance(rule.kind, str) and rule.kind
        assert isinstance(rule.when, dict)
        assert isinstance(rule.then, dict)
        assert isinstance(rule.precedence, int)


def test_lifecycle_rules_have_kind_lifecycle(canonical_pack: RulePack) -> None:
    """All rules in RulePack.lifecycle must have kind == 'lifecycle'."""
    for rule in canonical_pack.lifecycle:
        assert rule.kind == 'lifecycle', f'{rule.id} has kind={rule.kind}'


def test_risk_rules_have_kind_risk(canonical_pack: RulePack) -> None:
    """All rules in RulePack.risk must have kind == 'risk'."""
    for rule in canonical_pack.risk:
        assert rule.kind == 'risk', f'{rule.id} has kind={rule.kind}'


# ---------------------------------------------------------------------------
# 2. Mapping transformation
# ---------------------------------------------------------------------------


def test_mapping_ad_enabled(canonical_pack: RulePack) -> None:
    """mapping['ad']['enabled'] must equal the canonical AD enabled entry."""
    assert canonical_pack.mapping['ad']['enabled'] == {
        'concrete': 'userAccountControl=512',
        'actions': ['ensure_account', 'enable'],
    }


def test_mapping_github_no_suspended(canonical_pack: RulePack) -> None:
    """GitHub mapping must not have a 'suspended' entry."""
    assert 'suspended' not in canonical_pack.mapping['github']


def test_mapping_customer_portal_all_states(canonical_pack: RulePack) -> None:
    """customer_portal mapping must have all 5 abstract states."""
    assert set(canonical_pack.mapping['customer_portal'].keys()) == {
        'enabled',
        'disabled',
        'suspended',
        'grace',
        'pending',
    }


# ---------------------------------------------------------------------------
# 3. Error handling
# ---------------------------------------------------------------------------


def test_missing_lifecycle_yaml(tmp_path: Path) -> None:
    """Missing lifecycle.yaml must raise PolicyLoadError."""
    _minimal_risk(tmp_path)
    _minimal_mapping(tmp_path)
    with pytest.raises(PolicyLoadError, match='lifecycle.yaml'):
        load_rules_from_yaml(tmp_path)


def test_missing_rules_key(tmp_path: Path) -> None:
    """lifecycle.yaml without a 'rules' key must raise PolicyLoadError."""
    (tmp_path / 'lifecycle.yaml').write_text('foo: bar\n', encoding='utf-8')
    _minimal_risk(tmp_path)
    _minimal_mapping(tmp_path)
    with pytest.raises(PolicyLoadError):
        load_rules_from_yaml(tmp_path)


def test_invalid_rule_missing_id(tmp_path: Path) -> None:
    """A rule missing the 'id' field must raise PolicyLoadError."""
    _write_yaml(
        tmp_path / 'lifecycle.yaml',
        {
            'rules': [
                {
                    'kind': 'lifecycle',
                    'when': {'subject.kind': 'employee'},
                    'then': {'abstract_state': 'enabled'},
                    'precedence': 10,
                }
            ]
        },
    )
    _minimal_risk(tmp_path)
    _minimal_mapping(tmp_path)
    with pytest.raises(PolicyLoadError):
        load_rules_from_yaml(tmp_path)


def test_invalid_kind(tmp_path: Path) -> None:
    """A rule with kind='unknown' must raise PolicyLoadError."""
    _write_yaml(
        tmp_path / 'lifecycle.yaml',
        {
            'rules': [
                {
                    'id': 'bad_kind',
                    'kind': 'unknown',
                    'when': {'subject.kind': 'employee'},
                    'then': {'abstract_state': 'enabled'},
                    'precedence': 10,
                }
            ]
        },
    )
    _minimal_risk(tmp_path)
    _minimal_mapping(tmp_path)
    with pytest.raises(PolicyLoadError, match='unknown'):
        load_rules_from_yaml(tmp_path)


def test_duplicate_rule_id(tmp_path: Path) -> None:
    """Two rules with the same id in lifecycle.yaml must raise PolicyLoadError."""
    _write_yaml(
        tmp_path / 'lifecycle.yaml',
        {
            'rules': [
                {
                    'id': 'dup',
                    'kind': 'lifecycle',
                    'when': {'subject.kind': 'employee'},
                    'then': {'abstract_state': 'enabled'},
                    'precedence': 10,
                },
                {
                    'id': 'dup',
                    'kind': 'lifecycle',
                    'when': {'subject.kind': 'nhi'},
                    'then': {'abstract_state': 'disabled'},
                    'precedence': 20,
                },
            ]
        },
    )
    _minimal_risk(tmp_path)
    _minimal_mapping(tmp_path)
    with pytest.raises(PolicyLoadError, match='dup'):
        load_rules_from_yaml(tmp_path)


def test_malformed_yaml(tmp_path: Path) -> None:
    """Invalid YAML syntax must raise PolicyLoadError."""
    (tmp_path / 'lifecycle.yaml').write_text('rules:\n  - id: [unclosed bracket\n', encoding='utf-8')
    _minimal_risk(tmp_path)
    _minimal_mapping(tmp_path)
    with pytest.raises(PolicyLoadError):
        load_rules_from_yaml(tmp_path)


# ---------------------------------------------------------------------------
# 4. Integration: loader output feeds evaluator
# ---------------------------------------------------------------------------


def test_loaded_rules_feed_evaluator(canonical_pack: RulePack) -> None:
    """Loaded rules produce the expected result when fed into evaluate().

    A terminated employee with an AD target must get abstract_state=disabled
    from the canonical rule set — proves the YAML rules behave identically to
    the hardcoded rules used in earlier evaluator tests.
    """
    all_rules = canonical_pack.lifecycle + canonical_pack.risk
    facts = Facts(
        subject=SubjectFacts(id='emp-1', kind='employee', status='terminated'),
        target=TargetFacts(application='ad'),
        now=NOW,
    )
    decision = evaluate(all_rules, facts, mapping=canonical_pack.mapping)
    assert decision.abstract_state == AbstractState.disabled
