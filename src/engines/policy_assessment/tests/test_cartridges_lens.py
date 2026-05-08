# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Validation tests for Lens policy cartridges in cartridges/lens/."""

from pathlib import Path

import pytest
import yaml
from src.inventory.policy.enums import AssessmentStrategy, PolicyType

CARTRIDGES_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / 'cartridges' / 'lens'

_REQUIRED_KEYS = frozenset(
    {
        'id',
        'version',
        'name',
        'description',
        'policy_type',
        'rule_id',
        'assessment_strategy',
        'requires',
        'condition',
        'decision',
        'finding',
    }
)

_VALID_POLICY_TYPES = frozenset(pt.value for pt in PolicyType)
_VALID_STRATEGIES = frozenset(s.value for s in AssessmentStrategy)


def _collect_cartridge_paths() -> list[Path]:
    if not CARTRIDGES_DIR.exists():
        return []
    return sorted(CARTRIDGES_DIR.rglob('*.yaml'))


_CARTRIDGE_PATHS = _collect_cartridge_paths()


@pytest.mark.parametrize('path', _CARTRIDGE_PATHS, ids=lambda p: p.stem)
def test_cartridge_has_required_keys(path: Path) -> None:
    with path.open('rb') as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), f'{path.name}: top-level must be a mapping'
    missing = _REQUIRED_KEYS - data.keys()
    assert not missing, f'{path.name}: missing required keys {sorted(missing)}'


@pytest.mark.parametrize('path', _CARTRIDGE_PATHS, ids=lambda p: p.stem)
def test_cartridge_policy_type_is_valid(path: Path) -> None:
    with path.open('rb') as fh:
        data = yaml.safe_load(fh)
    policy_type = data.get('policy_type', '')
    assert policy_type in _VALID_POLICY_TYPES, (
        f'{path.name}: policy_type "{policy_type}" is not a valid PolicyType value; '
        f'must be one of {sorted(_VALID_POLICY_TYPES)}'
    )


@pytest.mark.parametrize('path', _CARTRIDGE_PATHS, ids=lambda p: p.stem)
def test_cartridge_rule_id_is_not_generic(path: Path) -> None:
    with path.open('rb') as fh:
        data = yaml.safe_load(fh)
    rule_id = data.get('rule_id', '')
    policy_type = data.get('policy_type', '')
    assert isinstance(rule_id, str) and rule_id, f'{path.name}: rule_id must be a non-empty string'
    assert rule_id != policy_type, (
        f'{path.name}: rule_id "{rule_id}" must not equal policy_type "{policy_type}"; '
        'use a specific identifier like "<namespace>.<policy_type>.<name>"'
    )


@pytest.mark.parametrize('path', _CARTRIDGE_PATHS, ids=lambda p: p.stem)
def test_cartridge_assessment_strategy_is_valid(path: Path) -> None:
    with path.open('rb') as fh:
        data = yaml.safe_load(fh)
    strategy = data.get('assessment_strategy', '')
    assert strategy in _VALID_STRATEGIES, (
        f'{path.name}: assessment_strategy "{strategy}" is not valid; must be one of {sorted(_VALID_STRATEGIES)}'
    )


def test_cartridges_directory_exists() -> None:
    assert CARTRIDGES_DIR.exists(), f'cartridges/lens/ directory not found at {CARTRIDGES_DIR}'


def test_runnable_lens_cartridges_present() -> None:
    """Only runnable Lens cartridges live under cartridges/lens/.

    SoD is DB-backed and intentionally has no runnable file cartridge here;
    its template lives under cartridges/templates/sod/ as documentation.
    """
    stems = {p.stem for p in _CARTRIDGE_PATHS}
    expected = {'orphaned_access', 'unused_access', 'terminated_subject_access', 'privileged_access'}
    assert expected <= stems, f'Missing cartridges: {expected - stems}'
    assert 'toxic_combination' not in stems, (
        'toxic_combination.yaml must not live under cartridges/lens/ — '
        'SoD is DB-backed; the YAML is a documentation template under cartridges/templates/sod/'
    )


def test_cartridges_grouped_by_policy_type() -> None:
    for path in _CARTRIDGE_PATHS:
        with path.open('rb') as fh:
            data = yaml.safe_load(fh)
        policy_type = data.get('policy_type', '')
        assert path.parent.name == policy_type, (
            f'{path.name}: parent directory is "{path.parent.name}" but policy_type is "{policy_type}"; '
            'cartridges must be placed in a directory matching their policy_type'
        )
