# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for FileCartridgeLoader — load_file, load_dir, manifest validation."""

from pathlib import Path

import pytest
import yaml
from src.inventory.policy.cartridges.loader import CartridgeLoadError, FileCartridgeLoader
from src.inventory.policy.cartridges.schemas import CartridgeManifest
from src.inventory.policy.enums import AssessmentStrategy, PolicyType

LENS_CARTRIDGES_DIR = Path(__file__).parent.parent.parent.parent.parent.parent.parent / 'cartridges' / 'lens'

_LOADER = FileCartridgeLoader()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session')
def lens_manifests() -> list[CartridgeManifest]:
    return _LOADER.load_dir(LENS_CARTRIDGES_DIR)


@pytest.fixture(scope='session')
def orphaned_access_manifest() -> CartridgeManifest:
    return _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')


# ---------------------------------------------------------------------------
# load_dir: all Lens cartridges load and validate
# ---------------------------------------------------------------------------


def test_load_dir_returns_runnable_manifests(lens_manifests: list[CartridgeManifest]) -> None:
    """Only runnable Lens cartridges are loaded.

    SoD is DB-backed; toxic_combination is documentation under cartridges/templates/sod/.
    """
    assert len(lens_manifests) == 4


def test_all_manifests_have_valid_policy_type(lens_manifests: list[CartridgeManifest]) -> None:
    valid = set(PolicyType)
    for m in lens_manifests:
        assert m.policy_type in valid, f'{m.id}: unexpected policy_type {m.policy_type}'


def test_all_manifests_have_valid_strategy(lens_manifests: list[CartridgeManifest]) -> None:
    valid = set(AssessmentStrategy)
    for m in lens_manifests:
        assert m.assessment_strategy in valid, f'{m.id}: unexpected strategy {m.assessment_strategy}'


def test_all_manifests_rule_id_differs_from_policy_type(lens_manifests: list[CartridgeManifest]) -> None:
    for m in lens_manifests:
        assert m.rule_id != m.policy_type.value, (
            f'{m.id}: rule_id must not equal policy_type value "{m.policy_type.value}"'
        )


def test_all_manifests_have_non_empty_ids(lens_manifests: list[CartridgeManifest]) -> None:
    for m in lens_manifests:
        assert m.id


def test_all_manifests_version_is_int(lens_manifests: list[CartridgeManifest]) -> None:
    for m in lens_manifests:
        assert isinstance(m.version, int), f'{m.id}: version must be int, got {type(m.version)}'


def test_expected_cartridge_ids_present(lens_manifests: list[CartridgeManifest]) -> None:
    ids = {m.id for m in lens_manifests}
    expected = {
        'lens.access_risk.orphaned_access',
        'lens.access_risk.unused_access',
        'lens.access_risk.privileged_access',
        'lens.lifecycle.terminated_subject_access',
    }
    assert expected == ids


# ---------------------------------------------------------------------------
# load_file: field-level checks on orphaned_access
# ---------------------------------------------------------------------------


def test_orphaned_access_fields(orphaned_access_manifest: CartridgeManifest) -> None:
    m = orphaned_access_manifest
    assert m.id == 'lens.access_risk.orphaned_access'
    assert m.version == 1
    assert m.policy_type == PolicyType.ACCESS_RISK
    assert m.assessment_strategy == AssessmentStrategy.DETERMINISTIC
    assert isinstance(m.requires, dict)
    assert isinstance(m.condition, dict)
    assert isinstance(m.decision, dict)
    assert isinstance(m.finding, dict)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_load_file_missing(tmp_path: Path) -> None:
    with pytest.raises(CartridgeLoadError, match='not found'):
        _LOADER.load_file(tmp_path / 'nonexistent.yaml')


def test_load_file_invalid_yaml(tmp_path: Path) -> None:
    bad = tmp_path / 'bad.yaml'
    bad.write_text('id: [unclosed bracket\n', encoding='utf-8')
    with pytest.raises(CartridgeLoadError):
        _LOADER.load_file(bad)


def test_load_file_not_a_mapping(tmp_path: Path) -> None:
    bad = tmp_path / 'list.yaml'
    bad.write_text('- item1\n- item2\n', encoding='utf-8')
    with pytest.raises(CartridgeLoadError, match='mapping'):
        _LOADER.load_file(bad)


def test_load_file_missing_required_field(tmp_path: Path) -> None:
    bad = tmp_path / 'incomplete.yaml'
    bad.write_text(
        yaml.dump({'id': 'x', 'version': 1, 'name': 'X'}),
        encoding='utf-8',
    )
    with pytest.raises(CartridgeLoadError, match='invalid cartridge manifest'):
        _LOADER.load_file(bad)


def test_load_file_invalid_policy_type(tmp_path: Path) -> None:
    bad = tmp_path / 'bad_type.yaml'
    bad.write_text(
        yaml.dump(
            {
                'id': 'x',
                'version': 1,
                'name': 'X',
                'policy_type': 'not_a_real_type',
                'rule_id': 'x.rule',
                'assessment_strategy': 'deterministic',
            }
        ),
        encoding='utf-8',
    )
    with pytest.raises(CartridgeLoadError, match='invalid cartridge manifest'):
        _LOADER.load_file(bad)


def test_load_dir_missing(tmp_path: Path) -> None:
    with pytest.raises(CartridgeLoadError, match='not found'):
        _LOADER.load_dir(tmp_path / 'does_not_exist')
