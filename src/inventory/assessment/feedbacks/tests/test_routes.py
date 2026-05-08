# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /feedbacks routes."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import uuid

import pytest
from src.inventory.access_model.capabilities.models import Capability
from src.inventory.access_model.capability_mappings.models import CapabilityMapping
from src.inventory.access_model.capability_scope_keys.models import CapabilityScopeKey
from src.inventory.assessment.findings.models import Finding, FindingKind, FindingStatus
from src.inventory.assessment.scan_runs.models import ScanRun, ScanRunTrigger
from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity

_BASE = '/api/v0/feedbacks'


# ---------------------------------------------------------------------------
# Helpers — insert test data outside the HTTP client
# ---------------------------------------------------------------------------


async def _insert_rule(session_factory) -> int:
    async with session_factory() as session:
        rule = SodRule(
            code=f'RT-FB-{uuid.uuid4().hex[:6]}',
            name='Route Feedback Rule',
            severity=SodSeverity.high,
            scope_mode=SodRuleScope.global_,
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        await session.commit()
    return rule_id


async def _insert_capability_mapping(session_factory) -> int:
    async with session_factory() as session:
        cap = Capability(
            slug=f'rt-fb-cap-{uuid.uuid4().hex[:6]}',
            name='Route Feedback Capability',
        )
        session.add(cap)
        await session.flush()

        scope_key = CapabilityScopeKey(
            code=f'RT-FB-SK-{uuid.uuid4().hex[:6]}',
            name='Route Feedback Scope Key',
        )
        session.add(scope_key)
        await session.flush()

        mapping = CapabilityMapping(
            capability_id=cap.id,
            scope_key_id=scope_key.id,
            resource_kind='test_resource',
            scope_value_source={'kind': 'constant', 'value': 'v'},
        )
        session.add(mapping)
        await session.flush()
        mapping_id = mapping.id
        await session.commit()
    return mapping_id


async def _insert_subject(session_factory) -> uuid.UUID:
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    async with session_factory() as session:
        nhi = await create_nhi(
            session,
            external_id=f'rt-fb-nhi-{uuid.uuid4().hex[:8]}',
            name='Route Feedback NHI',
            kind='service_account',
        )
        subject = await create_subject(
            session,
            external_id=f'rt-fb-subj-{uuid.uuid4().hex[:8]}',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.service_account,
            principal_nhi_id=nhi.id,
            status=SubjectNHIStatus.active,
        )
        subject_id = subject.id
        await session.commit()
    return subject_id


async def _insert_finding(session_factory, rule_id: int, subject_id: uuid.UUID) -> int:
    async with session_factory() as session:
        run = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run)
        await session.flush()

        h = hashlib.sha256(f'{rule_id}:{subject_id}:{uuid.uuid4().hex}'.encode()).hexdigest()[:64]
        finding = Finding(
            scan_run_id=run.id,
            kind=FindingKind.sod,
            subject_id=subject_id,
            account_id=None,
            rule_id=rule_id,
            scope_key_id=None,
            scope_value=None,
            severity=SodSeverity.high,
            status=FindingStatus.open,
            matched_capability_grant_ids=[],
            matched_effective_grant_ids=[],
            matched_access_fact_ids=[],
            evidence_hash=h,
            evaluated_at=datetime.now(UTC),
        )
        session.add(finding)
        await session.flush()
        finding_id = finding.id
        await session.commit()
    return finding_id


# ---------------------------------------------------------------------------
# POST /feedbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_feedbacks_valid_returns_201(client, session_factory) -> None:
    """POST /feedbacks with valid body returns 201 and FeedbackRead shape."""
    rule_id = await _insert_rule(session_factory)

    body = {
        'rule_id': rule_id,
        'kind': 'needs_rule_fix',
        'message': 'This rule needs updating',
        'created_by': 'alice@example.com',
    }
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data['rule_id'] == rule_id
    assert data['kind'] == 'needs_rule_fix'
    assert data['message'] == 'This rule needs updating'
    assert data['created_by'] == 'alice@example.com'
    assert 'id' in data
    assert data['id'] > 0
    assert 'created_at' in data


@pytest.mark.asyncio
async def test_post_feedbacks_empty_message_returns_422(client, session_factory) -> None:
    """POST /feedbacks with empty message → 422 (Pydantic min_length=1)."""
    rule_id = await _insert_rule(session_factory)

    body = {
        'rule_id': rule_id,
        'kind': 'needs_rule_fix',
        'message': '',
    }
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_feedbacks_message_over_4000_chars_returns_422(client, session_factory) -> None:
    """POST /feedbacks with message over 4000 chars → 422 (Pydantic max_length=4000)."""
    rule_id = await _insert_rule(session_factory)

    body = {
        'rule_id': rule_id,
        'kind': 'needs_rule_fix',
        'message': 'X' * 4001,
    }
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_feedbacks_no_target_fks_returns_422(client) -> None:
    """POST /feedbacks with no target FKs → 422 (FeedbackTargetMissingError)."""
    body = {
        'kind': 'accepted_risk',
        'message': 'I accept this risk',
    }
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_feedbacks_nonexistent_finding_returns_404(client) -> None:
    """POST /feedbacks with non-existent finding_id → 404."""
    body = {
        'finding_id': 999_999_999,
        'kind': 'false_positive',
        'message': 'ghost finding',
    }
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_feedbacks_nonexistent_rule_returns_404(client) -> None:
    """POST /feedbacks with non-existent rule_id → 404."""
    body = {
        'rule_id': 999_999_999,
        'kind': 'needs_rule_fix',
        'message': 'ghost rule',
    }
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /feedbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_feedbacks_returns_200_list(client, session_factory) -> None:
    """GET /feedbacks returns 200 and a list."""
    rule_id = await _insert_rule(session_factory)
    await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'needs_rule_fix', 'message': 'test'},
    )
    resp = await client.get(_BASE)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_feedbacks_filter_by_kind(client, session_factory) -> None:
    """GET /feedbacks?kind=false_positive returns only that kind."""
    rule_id = await _insert_rule(session_factory)
    await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'false_positive', 'message': 'fp'},
    )
    await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'accepted_risk', 'message': 'ar'},
    )
    resp = await client.get(f'{_BASE}?kind=false_positive')
    assert resp.status_code == 200
    data = resp.json()
    assert all(d['kind'] == 'false_positive' for d in data)


@pytest.mark.asyncio
async def test_get_feedbacks_filter_by_rule_id(client, session_factory) -> None:
    """GET /feedbacks?rule_id=X returns only feedbacks for that rule."""
    rule_id = await _insert_rule(session_factory)
    rule_id2 = await _insert_rule(session_factory)
    await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'needs_rule_fix', 'message': 'r1'},
    )
    await client.post(
        _BASE,
        json={'rule_id': rule_id2, 'kind': 'needs_rule_fix', 'message': 'r2'},
    )
    resp = await client.get(f'{_BASE}?rule_id={rule_id}')
    assert resp.status_code == 200
    data = resp.json()
    assert all(d['rule_id'] == rule_id for d in data)


@pytest.mark.asyncio
async def test_get_feedbacks_filter_by_finding_id(client, session_factory) -> None:
    """GET /feedbacks?finding_id=X returns only feedbacks for that finding."""
    rule_id = await _insert_rule(session_factory)
    subject_id = await _insert_subject(session_factory)
    finding_id = await _insert_finding(session_factory, rule_id, subject_id)
    await client.post(
        _BASE,
        json={'finding_id': finding_id, 'kind': 'false_positive', 'message': 'fp'},
    )
    await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'needs_rule_fix', 'message': 'other'},
    )
    resp = await client.get(f'{_BASE}?finding_id={finding_id}')
    assert resp.status_code == 200
    data = resp.json()
    assert all(d['finding_id'] == finding_id for d in data)


# ---------------------------------------------------------------------------
# GET /feedbacks/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_feedback_by_id_returns_200(client, session_factory) -> None:
    """GET /feedbacks/{id} returns 200 for existing feedback."""
    rule_id = await _insert_rule(session_factory)
    post_resp = await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'needs_rule_fix', 'message': 'find me'},
    )
    assert post_resp.status_code == 201
    feedback_id = post_resp.json()['id']

    resp = await client.get(f'{_BASE}/{feedback_id}')
    assert resp.status_code == 200
    data = resp.json()
    assert data['id'] == feedback_id
    assert data['rule_id'] == rule_id


@pytest.mark.asyncio
async def test_get_feedback_by_id_returns_404_for_missing(client) -> None:
    """GET /feedbacks/{id} returns 404 for non-existent id."""
    resp = await client.get(f'{_BASE}/999999999')
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Negative: no PATCH / DELETE endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_patch_route_registered(client, session_factory) -> None:
    """PATCH /feedbacks/{id} is not registered — 405 or 404 from OpenAPI."""
    rule_id = await _insert_rule(session_factory)
    post_resp = await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'needs_rule_fix', 'message': 'check'},
    )
    feedback_id = post_resp.json()['id']

    resp = await client.patch(f'{_BASE}/{feedback_id}', json={'message': 'updated'})
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_no_delete_route_registered(client, session_factory) -> None:
    """DELETE /feedbacks/{id} is not registered — 405 or 404 from OpenAPI."""
    rule_id = await _insert_rule(session_factory)
    post_resp = await client.post(
        _BASE,
        json={'rule_id': rule_id, 'kind': 'needs_rule_fix', 'message': 'check'},
    )
    feedback_id = post_resp.json()['id']

    resp = await client.delete(f'{_BASE}/{feedback_id}')
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_openapi_does_not_list_patch_delete_for_feedbacks(client) -> None:
    """OpenAPI spec does not expose PATCH or DELETE on /feedbacks/{id}."""
    resp = await client.get('/openapi.json')
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec.get('paths', {})
    feedback_path = paths.get('/api/v0/feedbacks/{feedback_id}', {})
    assert 'patch' not in feedback_path
    assert 'delete' not in feedback_path
