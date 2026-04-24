# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure unit tests for capability_projector — no DB, no I/O."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.capabilities.access_analysis.capability_grants.capability_projector import (
    CapabilityMappingView,
    EffectiveGrantView,
    project_grant,
)

NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
GLOBAL_SK_ID = 1

APP_ID_A = uuid.uuid4()
APP_ID_B = uuid.uuid4()
RESOURCE_ID_A = uuid.uuid4()
GRANT_ID = uuid.uuid4()
SUBJECT_ID = uuid.uuid4()
CAP_ID = 10
SK_ID = 2
MAPPING_ID = 100


def _grant(
    *,
    resource_id: uuid.UUID = RESOURCE_ID_A,
    resource_kind: str = 'role',
    resource_external_id: str = 'arn:aws:iam::123456789012:role/admin',
    action_slug: str = 'read',
    application_id: uuid.UUID = APP_ID_A,
    tombstoned_at=None,
    resource_attributes: dict | None = None,
    subject_attributes: dict | None = None,
) -> EffectiveGrantView:
    return EffectiveGrantView(
        id=GRANT_ID,
        subject_id=SUBJECT_ID,
        application_id=application_id,
        resource_id=resource_id,
        action_slug=action_slug,
        tombstoned_at=tombstoned_at,
        resource_kind=resource_kind,
        resource_external_id=resource_external_id,
        resource_attributes=resource_attributes or {},
        subject_attributes=subject_attributes or {},
    )


def _mapping(
    *,
    id: int = MAPPING_ID,
    capability_id: int = CAP_ID,
    application_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    resource_kind: str | None = 'role',
    resource_path_glob: str | None = None,
    action_slug: str | None = None,
    scope_key_id: int = SK_ID,
    scope_value_source: dict | None = None,
    is_active: bool = True,
) -> CapabilityMappingView:
    return CapabilityMappingView(
        id=id,
        capability_id=capability_id,
        application_id=application_id,
        resource_id=resource_id,
        resource_kind=resource_kind,
        resource_path_glob=resource_path_glob,
        action_slug=action_slug,
        scope_key_id=scope_key_id,
        scope_value_source=scope_value_source or {'kind': 'constant', 'value': 'admin'},
        is_active=is_active,
    )


# ---------------------------------------------------------------------------
# Resource match tests
# ---------------------------------------------------------------------------


def test_resource_id_match_emits_one_draft() -> None:
    grant = _grant(resource_id=RESOURCE_ID_A)
    m = _mapping(resource_id=RESOURCE_ID_A, resource_kind=None)
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].source_effective_grant_id == GRANT_ID


def test_resource_kind_match_emits_one_draft() -> None:
    grant = _grant(resource_kind='account')
    m = _mapping(resource_kind='account')
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1


def test_resource_path_glob_match_uses_fnmatchcase() -> None:
    # case-sensitive match
    grant = _grant(resource_external_id='arn:aws:iam::123456789012:role/admin', resource_kind='role')
    m = _mapping(resource_path_glob='arn:aws:iam::*:role/admin', resource_kind=None)
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1

    # case mismatch — fnmatchcase should NOT match
    grant_upper = _grant(resource_external_id='ARN:AWS:IAM::123456789012:ROLE/ADMIN', resource_kind='role')
    drafts_upper = project_grant(grant_upper, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts_upper) == 0


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------


def test_application_id_filter_skips_unmatched_grant() -> None:
    grant = _grant(application_id=APP_ID_B)
    m = _mapping(application_id=APP_ID_A)  # only matches APP_ID_A
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert drafts == []


def test_action_slug_filter_skips_unmatched_grant() -> None:
    grant = _grant(action_slug='write')
    m = _mapping(action_slug='read')  # only matches 'read'
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert drafts == []


# ---------------------------------------------------------------------------
# Scope value source tests
# ---------------------------------------------------------------------------


def test_subject_attribute_source_resolves_value() -> None:
    grant = _grant(subject_attributes={'cost_center': 'CC-001'})
    m = _mapping(scope_value_source={'kind': 'subject_attribute', 'key': 'cost_center'})
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].scope_value == 'cc-001'  # lowercased


def test_subject_attribute_missing_yields_null_scope_value() -> None:
    grant = _grant(subject_attributes={})
    m = _mapping(scope_value_source={'kind': 'subject_attribute', 'key': 'cost_center'})
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].scope_value is None


def test_resource_attribute_source_resolves_value() -> None:
    grant = _grant(resource_attributes={'env': 'Prod'})
    m = _mapping(scope_value_source={'kind': 'resource_attribute', 'key': 'env'})
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].scope_value == 'prod'  # lowercased


def test_resource_attribute_missing_yields_null_scope_value() -> None:
    grant = _grant(resource_attributes={})
    m = _mapping(scope_value_source={'kind': 'resource_attribute', 'key': 'env'})
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].scope_value is None


def test_application_id_source_resolves_to_grant_application_id() -> None:
    grant = _grant(application_id=APP_ID_A)
    m = _mapping(scope_value_source={'kind': 'application_id'})
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].scope_value == str(APP_ID_A).lower()


def test_constant_source_passes_through_value() -> None:
    m = _mapping(scope_value_source={'kind': 'constant', 'value': '  Admin  '})
    drafts = project_grant(_grant(), [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].scope_value == 'admin'  # stripped + lowercased


def test_global_scope_key_forces_scope_value_to_none_regardless_of_source() -> None:
    m = _mapping(scope_key_id=GLOBAL_SK_ID, scope_value_source={'kind': 'constant', 'value': 'X'})
    drafts = project_grant(_grant(), [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].scope_value is None  # forced to None for GLOBAL


# ---------------------------------------------------------------------------
# Determinism / ordering tests
# ---------------------------------------------------------------------------


def test_multiple_matching_mappings_produce_distinct_drafts_in_deterministic_order() -> None:
    mapping_a = _mapping(id=1, capability_id=10, scope_key_id=2, scope_value_source={'kind': 'constant', 'value': 'z'})
    mapping_b = _mapping(id=2, capability_id=10, scope_key_id=2, scope_value_source={'kind': 'constant', 'value': 'a'})
    mapping_c = _mapping(id=3, capability_id=10, scope_key_id=2, scope_value_source={'kind': 'constant', 'value': 'm'})

    drafts1 = project_grant(_grant(), [mapping_a, mapping_b, mapping_c], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    drafts2 = project_grant(_grant(), [mapping_c, mapping_a, mapping_b], now=NOW, global_scope_key_id=GLOBAL_SK_ID)

    assert len(drafts1) == 3
    # Sorted by (source_capability_mapping_id, capability_id, scope_key_id, scope_value or '',
    # source_effective_grant_id)
    assert [d.source_capability_mapping_id for d in drafts1] == [1, 2, 3]
    assert [d.source_capability_mapping_id for d in drafts2] == [1, 2, 3]

    # Byte-identical on repeated call
    assert [d.scope_value for d in drafts1] == [d.scope_value for d in drafts2]


# ---------------------------------------------------------------------------
# Tombstoning / inactive mapping tests
# ---------------------------------------------------------------------------


def test_tombstoned_grant_yields_tombstoned_drafts() -> None:
    tombstone_ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    grant = _grant(tombstoned_at=tombstone_ts)
    m = _mapping()
    drafts = project_grant(grant, [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert len(drafts) == 1
    assert drafts[0].tombstoned_at == tombstone_ts


def test_inactive_mapping_is_skipped() -> None:
    m = _mapping(is_active=False)
    drafts = project_grant(_grant(), [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
    assert drafts == []


def test_corrupted_mapping_with_no_resource_match_raises_value_error() -> None:
    m = _mapping(resource_id=None, resource_kind=None, resource_path_glob=None)
    with pytest.raises(ValueError, match='has no resource_id'):
        project_grant(_grant(), [m], now=NOW, global_scope_key_id=GLOBAL_SK_ID)
