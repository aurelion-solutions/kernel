# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for cartridge_manifest_to_request adapter."""

from pathlib import Path

from src.engines.policy_assessment.cartridge_adapter import cartridge_manifest_to_request
from src.engines.policy_assessment.contracts import PolicyAssessmentRequest
from src.inventory.policy.cartridges.loader import FileCartridgeLoader
from src.inventory.policy.enums import AssessmentStrategy, PolicyType

LENS_CARTRIDGES_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / 'cartridges' / 'lens'

_LOADER = FileCartridgeLoader()


def test_adapter_returns_policy_assessment_request() -> None:
    manifest = _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')
    req = cartridge_manifest_to_request(manifest, {})
    assert isinstance(req, PolicyAssessmentRequest)


def test_adapter_maps_policy_type() -> None:
    manifest = _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')
    req = cartridge_manifest_to_request(manifest, {})
    assert req.policy_type == PolicyType.ACCESS_RISK


def test_adapter_maps_assessment_strategy() -> None:
    manifest = _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')
    req = cartridge_manifest_to_request(manifest, {})
    assert req.assessment_strategy == AssessmentStrategy.DETERMINISTIC


def test_adapter_maps_policy_id() -> None:
    manifest = _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')
    req = cartridge_manifest_to_request(manifest, {})
    assert req.policy_id == 'lens.access_risk.orphaned_access'


def test_adapter_maps_context() -> None:
    manifest = _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')
    ctx = {'subject_id': 'emp-42', 'account_status': 'active'}
    req = cartridge_manifest_to_request(manifest, ctx)
    assert req.context == ctx


def test_adapter_policy_definition_contains_manifest_fields() -> None:
    manifest = _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')
    req = cartridge_manifest_to_request(manifest, {})
    assert req.policy_definition['id'] == 'lens.access_risk.orphaned_access'
    assert req.policy_definition['policy_type'] == 'access_risk'
    assert req.policy_definition['version'] == 1


def test_adapter_context_isolation() -> None:
    manifest = _LOADER.load_file(LENS_CARTRIDGES_DIR / 'access_risk' / 'orphaned_access.yaml')
    req_a = cartridge_manifest_to_request(manifest, {'subject_id': 'emp-1'})
    req_b = cartridge_manifest_to_request(manifest, {'subject_id': 'emp-2'})
    assert req_a.context['subject_id'] == 'emp-1'
    assert req_b.context['subject_id'] == 'emp-2'


def test_adapter_works_for_all_cartridges() -> None:
    manifests = _LOADER.load_dir(LENS_CARTRIDGES_DIR)
    for manifest in manifests:
        req = cartridge_manifest_to_request(manifest, {'subject_id': 'test'})
        assert req.policy_id == manifest.id
        assert req.policy_type == manifest.policy_type
        assert req.assessment_strategy == manifest.assessment_strategy
