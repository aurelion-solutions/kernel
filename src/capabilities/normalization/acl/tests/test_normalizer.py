# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for the pure ACL normalizer function."""

from __future__ import annotations

import pytest
from src.capabilities.normalization.acl.normalizer import ACLPayloadError, normalize_acl_entry
from src.capabilities.normalization.acl.schemas import ACLEntryPayload
from src.inventory.access_facts.schemas import AccessFactEffect
from src.inventory.enums import Action
from src.inventory.resources.models import ResourceDataSensitivity, ResourceEnvironment, ResourcePrivilegeLevel


def _make_payload(**kwargs) -> ACLEntryPayload:
    defaults = {
        'resource_external_id': '/repo/core/src',
        'resource_kind': 'folder',
        'verb': 'read',
        'effect': 'allow',
    }
    defaults.update(kwargs)
    return ACLEntryPayload(**defaults)


def test_read_verb_maps_to_action_read_and_privilege_read() -> None:
    result = normalize_acl_entry(_make_payload(verb='read'))
    assert result.action == Action.read
    assert result.privilege_level == ResourcePrivilegeLevel.read


def test_write_verb_maps_to_action_write_and_privilege_write() -> None:
    result = normalize_acl_entry(_make_payload(verb='write'))
    assert result.action == Action.write
    assert result.privilege_level == ResourcePrivilegeLevel.write


def test_admin_verb_maps_to_action_administer_and_privilege_admin() -> None:
    result = normalize_acl_entry(_make_payload(verb='admin'))
    assert result.action == Action.administer
    assert result.privilege_level == ResourcePrivilegeLevel.admin


def test_allow_effect_is_preserved() -> None:
    result = normalize_acl_entry(_make_payload(effect='allow'))
    assert result.effect == AccessFactEffect.allow


def test_deny_effect_is_preserved() -> None:
    result = normalize_acl_entry(_make_payload(effect='deny'))
    assert result.effect == AccessFactEffect.deny


def test_environment_pass_through_when_set() -> None:
    result = normalize_acl_entry(_make_payload(environment='staging'))
    assert result.environment == ResourceEnvironment.staging


def test_data_sensitivity_pass_through_when_set() -> None:
    result = normalize_acl_entry(_make_payload(data_sensitivity='pii'))
    assert result.data_sensitivity == ResourceDataSensitivity.pii


def test_environment_is_none_when_absent() -> None:
    result = normalize_acl_entry(_make_payload(environment=None))
    assert result.environment is None


def test_unknown_verb_raises_acl_payload_error() -> None:
    # Bypass Pydantic Literal validation via model_construct.
    payload = ACLEntryPayload.model_construct(
        resource_external_id='/repo/core/src',
        resource_kind='folder',
        verb='delete',  # not in Literal["read", "write", "admin"]
        effect='allow',
    )
    with pytest.raises(ACLPayloadError):
        normalize_acl_entry(payload)


def test_empty_resource_external_id_raises_acl_payload_error() -> None:
    # Bypass Pydantic field_validator via model_construct.
    payload = ACLEntryPayload.model_construct(
        resource_external_id='   ',
        resource_kind='folder',
        verb='read',
        effect='allow',
    )
    with pytest.raises(ACLPayloadError):
        normalize_acl_entry(payload)
