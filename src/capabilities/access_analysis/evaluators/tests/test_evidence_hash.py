# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Focused tests on evidence_hash stability and canonicalization."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from src.capabilities.access_analysis.evaluators.sod import (
    CapabilityGrantView,
    _compute_evidence_hash,
    _serialize_canonical_json,
)

_AT = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_SUBJECT = UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
_SUBJECT2 = UUID('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
_APP = uuid4()


def _grant(
    gid: int,
    cap_id: int,
    cap_slug: str,
    eg_id: UUID,
    mapping_id: int = 1,
    access_fact_ids: list[int] | None = None,
    initiative_ids: list[int] | None = None,
) -> CapabilityGrantView:
    return CapabilityGrantView(
        id=gid,
        subject_id=_SUBJECT,
        capability_id=cap_id,
        capability_slug=cap_slug,
        scope_key_id=1,
        scope_value=None,
        application_id=_APP,
        source_effective_grant_id=eg_id,
        source_capability_mapping_id=mapping_id,
        source_access_fact_ids=access_fact_ids or [gid * 10],
        source_initiative_ids=initiative_ids or [gid * 20],
    )


# ---------------------------------------------------------------------------
# Test 19: Hash stability across EffectiveGrant.id change
# ---------------------------------------------------------------------------


def test_hash_stable_across_effective_grant_id_change() -> None:
    """Changing source_effective_grant_id leaves evidence_hash unchanged.

    Only stable IDs (access_fact, initiative, capability_mapping) feed the hash.
    """
    eg_id_v1 = UUID('11111111-1111-1111-1111-111111111111')
    eg_id_v2 = UUID('22222222-2222-2222-2222-222222222222')

    g1 = _grant(1, 10, 'approve_payment', eg_id=eg_id_v1, mapping_id=5, access_fact_ids=[1001], initiative_ids=[2001])
    g2 = _grant(1, 10, 'approve_payment', eg_id=eg_id_v2, mapping_id=5, access_fact_ids=[1001], initiative_ids=[2001])

    h1 = _compute_evidence_hash(_SUBJECT, 1, None, None, [g1])
    h2 = _compute_evidence_hash(_SUBJECT, 1, None, None, [g2])

    assert h1 == h2


# ---------------------------------------------------------------------------
# Test 20: Hash sensitivity to stable IDs
# ---------------------------------------------------------------------------


def test_hash_sensitive_to_stable_ids() -> None:
    """Changing any stable ID changes the hash."""
    eg_id = UUID('11111111-1111-1111-1111-111111111111')

    base = _grant(1, 10, 'approve_payment', eg_id=eg_id, mapping_id=5, access_fact_ids=[1001], initiative_ids=[2001])
    diff_fact = _grant(
        1, 10, 'approve_payment', eg_id=eg_id, mapping_id=5, access_fact_ids=[9999], initiative_ids=[2001]
    )
    diff_init = _grant(
        1, 10, 'approve_payment', eg_id=eg_id, mapping_id=5, access_fact_ids=[1001], initiative_ids=[9999]
    )
    diff_map = _grant(
        1, 10, 'approve_payment', eg_id=eg_id, mapping_id=99, access_fact_ids=[1001], initiative_ids=[2001]
    )

    h_base = _compute_evidence_hash(_SUBJECT, 1, None, None, [base])
    h_fact = _compute_evidence_hash(_SUBJECT, 1, None, None, [diff_fact])
    h_init = _compute_evidence_hash(_SUBJECT, 1, None, None, [diff_init])
    h_map = _compute_evidence_hash(_SUBJECT, 1, None, None, [diff_map])

    assert h_base != h_fact
    assert h_base != h_init
    assert h_base != h_map


# ---------------------------------------------------------------------------
# Test 21: Null vs empty-string scope_value → different hashes
# ---------------------------------------------------------------------------


def test_null_vs_empty_string_scope_value() -> None:
    """scope_value=None (JSON null) != scope_value='' (empty string) → different hashes."""
    eg_id = UUID('11111111-1111-1111-1111-111111111111')
    g = _grant(1, 10, 'approve_payment', eg_id=eg_id, access_fact_ids=[1001], initiative_ids=[2001])

    h_null = _compute_evidence_hash(_SUBJECT, 1, None, None, [g])
    h_empty = _compute_evidence_hash(_SUBJECT, 1, None, '', [g])

    assert h_null != h_empty

    # Also verify serialization difference
    s_null = _serialize_canonical_json(
        subject_id=str(_SUBJECT),
        access_fact_ids=[1001],
        initiative_ids=[2001],
        capability_mapping_ids=[1],
        rule_id=1,
        scope_key_id=None,
        scope_value=None,
    )
    s_empty = _serialize_canonical_json(
        subject_id=str(_SUBJECT),
        access_fact_ids=[1001],
        initiative_ids=[2001],
        capability_mapping_ids=[1],
        rule_id=1,
        scope_key_id=None,
        scope_value='',
    )
    assert 'null' in s_null
    assert s_null != s_empty


# ---------------------------------------------------------------------------
# Test 22: Hash sensitivity to subject
# ---------------------------------------------------------------------------


def test_hash_sensitive_to_subject() -> None:
    """Same access pattern under two different subjects → different hashes."""
    eg_id = UUID('11111111-1111-1111-1111-111111111111')
    g = _grant(1, 10, 'approve_payment', eg_id=eg_id, access_fact_ids=[1001], initiative_ids=[2001])

    h1 = _compute_evidence_hash(_SUBJECT, 1, None, None, [g])
    h2 = _compute_evidence_hash(_SUBJECT2, 1, None, None, [g])

    assert h1 != h2


# ---------------------------------------------------------------------------
# Test 23: Sorted-list canonicalization — different input orders → same hash
# ---------------------------------------------------------------------------


def test_sorted_list_canonicalization() -> None:
    """Same stable-ID sets in different grant order → identical hash."""
    eg_a = UUID('aaaaaaaa-0000-0000-0000-000000000001')
    eg_b = UUID('aaaaaaaa-0000-0000-0000-000000000002')

    g1 = _grant(1, 10, 'approve_payment', eg_id=eg_a, mapping_id=1, access_fact_ids=[100], initiative_ids=[200])
    g2 = _grant(2, 20, 'create_vendor', eg_id=eg_b, mapping_id=2, access_fact_ids=[300], initiative_ids=[400])

    h_order1 = _compute_evidence_hash(_SUBJECT, 1, None, None, [g1, g2])
    h_order2 = _compute_evidence_hash(_SUBJECT, 1, None, None, [g2, g1])

    assert h_order1 == h_order2
