# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Schema-level tests for CapabilityMapping Pydantic models."""

from __future__ import annotations

import uuid

from pydantic import ValidationError
import pytest
from src.inventory.access_model.capability_mappings.schemas import (
    CapabilityMappingCreate,
    ScopeValueSource,
)


def _base_payload(**overrides) -> dict:
    return {
        'capability_id': 1,
        'resource_kind': 'role',
        'scope_value_source': {'kind': 'constant', 'value': 'admin'},
        **overrides,
    }


# ---------------------------------------------------------------------------
# XOR resource match validation
# ---------------------------------------------------------------------------


def test_create_validates_resource_match_xor_two_set() -> None:
    """CapabilityMappingCreate with two resource-match fields raises ValidationError."""
    with pytest.raises(ValidationError):
        CapabilityMappingCreate(
            **_base_payload(
                resource_id=uuid.uuid4(),
                resource_kind='role',  # two set
            )
        )


def test_create_validates_resource_match_xor_zero_set() -> None:
    """CapabilityMappingCreate with no resource-match fields raises ValidationError."""
    with pytest.raises(ValidationError):
        CapabilityMappingCreate(
            **_base_payload(
                resource_kind=None,  # all None
            )
        )


def test_create_validates_resource_match_xor_one_set_succeeds() -> None:
    """CapabilityMappingCreate with exactly one resource-match field succeeds."""
    m = CapabilityMappingCreate(**_base_payload())
    assert m.resource_kind == 'role'
    assert m.resource_id is None
    assert m.resource_path_glob is None


# ---------------------------------------------------------------------------
# ScopeValueSource discriminated union
# ---------------------------------------------------------------------------


def test_scope_value_source_discriminator_rejects_unknown_kind() -> None:
    """scope_value_source with unknown kind raises ValidationError."""
    from pydantic import TypeAdapter

    ta = TypeAdapter(ScopeValueSource)
    with pytest.raises(ValidationError):
        ta.validate_python({'kind': 'env_var', 'value': 'x'})


@pytest.mark.parametrize(
    'payload',
    [
        {'kind': 'subject_attribute', 'key': 'department'},
        {'kind': 'resource_attribute', 'key': 'owner'},
        {'kind': 'application_id'},
        {'kind': 'constant', 'value': 'finance'},
    ],
)
def test_scope_value_source_discriminator_accepts_all_four_kinds(payload: dict) -> None:
    """All four valid kinds parse cleanly and round-trip via model_dump(mode='json')."""
    from pydantic import TypeAdapter

    ta = TypeAdapter(ScopeValueSource)
    parsed = ta.validate_python(payload)
    dumped = parsed.model_dump(mode='json')
    re_parsed = ta.validate_python(dumped)
    assert re_parsed.kind == payload['kind']


def test_read_with_corrupted_scope_value_source_raises_validation_error() -> None:
    """CapabilityMappingRead.model_validate with an invalid scope_value_source dict raises ValidationError.

    Documents that a manual SQL UPDATE bypassing the API can corrupt this field,
    causing ValidationError on read. The write-path enforces the shape; the DB does not.
    """
    import datetime

    from src.inventory.access_model.capability_mappings.schemas import CapabilityMappingRead

    with pytest.raises(ValidationError):
        CapabilityMappingRead.model_validate(
            {
                'id': 1,
                'capability_id': 1,
                'application_id': None,
                'resource_id': None,
                'resource_kind': 'role',
                'resource_path_glob': None,
                'action_slug': None,
                'scope_key_id': 1,
                'scope_value_source': {'kind': 'INVALID_KIND', 'value': 'x'},
                'is_active': True,
                'created_at': datetime.datetime.now(tz=datetime.UTC),
                'created_by': None,
            }
        )
