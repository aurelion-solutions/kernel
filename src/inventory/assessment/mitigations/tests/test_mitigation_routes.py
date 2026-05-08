# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /mitigations routes."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.assessment.mitigation_controls.models import MitigationControl, MitigationControlType
from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity

_BASE = '/api/v0/mitigations'


# ---------------------------------------------------------------------------
# Helpers — insert test data outside the HTTP client (direct session writes)
# ---------------------------------------------------------------------------


async def _insert_rule(session_factory, *, mitigation_allowed: bool = True) -> int:
    async with session_factory() as session:
        rule = SodRule(
            code=f'RT-RULE-{uuid.uuid4().hex[:6]}',
            name='Route Test Rule',
            severity=SodSeverity.high,
            scope_mode=SodRuleScope.global_,
            mitigation_allowed=mitigation_allowed,
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        await session.commit()
    return rule_id


async def _insert_control(session_factory, *, is_active: bool = True) -> int:
    async with session_factory() as session:
        ctrl = MitigationControl(
            code=f'RT-CTRL-{uuid.uuid4().hex[:6]}',
            name='Route Test Control',
            type=MitigationControlType.attestation,
            is_active=is_active,
        )
        session.add(ctrl)
        await session.flush()
        ctrl_id = ctrl.id
        await session.commit()
    return ctrl_id


async def _insert_subject(session_factory) -> uuid.UUID:
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    async with session_factory() as session:
        nhi = await create_nhi(
            session,
            external_id=f'rt-nhi-{uuid.uuid4().hex[:8]}',
            name='Route NHI',
            kind='service_account',
        )
        subject = await create_subject(
            session,
            external_id=f'rt-subj-{uuid.uuid4().hex[:8]}',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.service_account,
            principal_nhi_id=nhi.id,
            status=SubjectNHIStatus.active,
        )
        subject_id = subject.id
        await session.commit()
    return subject_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _valid_body(rule_id: int, ctrl_id: int, subject_id: uuid.UUID, owner_id: uuid.UUID) -> dict:
    return {
        'rule_id': rule_id,
        'control_id': ctrl_id,
        'subject_id': str(subject_id),
        'owner_id': str(owner_id),
        'valid_from': _now_iso(),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_mitigations_valid_returns_201(client, session_factory) -> None:
    """POST /mitigations with valid body returns 201 and response shape."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    body = _valid_body(rule_id, ctrl_id, subject_id, owner_id)
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 201
    data = resp.json()
    assert data['rule_id'] == rule_id
    assert data['control_id'] == ctrl_id
    assert data['status'] == 'proposed'
    assert 'id' in data
    assert data['id'] > 0


@pytest.mark.asyncio
async def test_post_mitigations_non_mitigatable_rule_returns_422(client, session_factory) -> None:
    """POST /mitigations against non-mitigatable rule returns 422."""
    rule_id = await _insert_rule(session_factory, mitigation_allowed=False)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    body = _valid_body(rule_id, ctrl_id, subject_id, owner_id)
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_mitigations_extra_field_returns_422(client, session_factory) -> None:
    """POST /mitigations with unknown extra field returns 422 (extra='forbid')."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    body = {**_valid_body(rule_id, ctrl_id, subject_id, owner_id), 'unexpected_field': 'oops'}
    resp = await client.post(_BASE, json=body)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_mitigations_no_filter_returns_200(client, session_factory) -> None:
    """GET /mitigations no filter returns 200 list."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    await client.post(_BASE, json=_valid_body(rule_id, ctrl_id, subject_id, owner_id))
    resp = await client.get(_BASE)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_get_mitigations_filter_rule_and_status(client, session_factory) -> None:
    """GET /mitigations?rule_id=...&status=active returns only matching rows."""
    rule_id = await _insert_rule(session_factory)
    rule_id_other = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)
    subject_id2 = await _insert_subject(session_factory)

    # Create one proposed for rule_id
    r1 = await client.post(_BASE, json=_valid_body(rule_id, ctrl_id, subject_id, owner_id))
    assert r1.status_code == 201
    mid = r1.json()['id']

    # Activate it
    await client.post(f'{_BASE}/{mid}/activate')

    # Create one proposed for other rule with different subject
    await client.post(_BASE, json=_valid_body(rule_id_other, ctrl_id, subject_id2, owner_id))

    resp = await client.get(f'{_BASE}?rule_id={rule_id}&status=active')
    assert resp.status_code == 200
    items = resp.json()
    assert all(i['rule_id'] == rule_id and i['status'] == 'active' for i in items)
    assert any(i['id'] == mid for i in items)


@pytest.mark.asyncio
async def test_get_mitigation_by_id_missing_returns_404(client) -> None:
    """GET /mitigations/{id} missing returns 404."""
    resp = await client.get(f'{_BASE}/99999')
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_activate_mitigation_happy_path(client, session_factory) -> None:
    """POST /mitigations/{id}/activate transitions proposed → active, returns 200."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    create_resp = await client.post(_BASE, json=_valid_body(rule_id, ctrl_id, subject_id, owner_id))
    assert create_resp.status_code == 201
    mid = create_resp.json()['id']

    activate_resp = await client.post(f'{_BASE}/{mid}/activate')
    assert activate_resp.status_code == 200
    assert activate_resp.json()['status'] == 'active'


@pytest.mark.asyncio
async def test_activate_already_active_returns_409(client, session_factory) -> None:
    """POST /mitigations/{id}/activate on already-active returns 409."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    create_resp = await client.post(_BASE, json=_valid_body(rule_id, ctrl_id, subject_id, owner_id))
    mid = create_resp.json()['id']

    await client.post(f'{_BASE}/{mid}/activate')
    second_activate = await client.post(f'{_BASE}/{mid}/activate')
    assert second_activate.status_code == 409


@pytest.mark.asyncio
async def test_revoke_mitigation_happy_path(client, session_factory) -> None:
    """POST /mitigations/{id}/revoke with reason returns 200 with status=revoked."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    create_resp = await client.post(_BASE, json=_valid_body(rule_id, ctrl_id, subject_id, owner_id))
    mid = create_resp.json()['id']

    revoke_resp = await client.post(f'{_BASE}/{mid}/revoke', json={'reason': 'employee left'})
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()['status'] == 'revoked'
    assert revoke_resp.json()['reason'] == 'employee left'


@pytest.mark.asyncio
async def test_revoke_missing_reason_returns_422(client, session_factory) -> None:
    """POST /mitigations/{id}/revoke without reason returns 422."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    create_resp = await client.post(_BASE, json=_valid_body(rule_id, ctrl_id, subject_id, owner_id))
    mid = create_resp.json()['id']

    revoke_resp = await client.post(f'{_BASE}/{mid}/revoke', json={})
    assert revoke_resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_status_invalid_transition_returns_409(client, session_factory) -> None:
    """PATCH /mitigations/{id}/status with invalid transition returns 409."""
    rule_id = await _insert_rule(session_factory)
    ctrl_id = await _insert_control(session_factory)
    subject_id = await _insert_subject(session_factory)
    owner_id = await _insert_subject(session_factory)

    create_resp = await client.post(_BASE, json=_valid_body(rule_id, ctrl_id, subject_id, owner_id))
    mid = create_resp.json()['id']

    # Revoke first, then try to re-activate (terminal state)
    await client.post(f'{_BASE}/{mid}/revoke', json={'reason': 'test'})
    patch_resp = await client.patch(f'{_BASE}/{mid}/status', json={'status': 'active'})
    assert patch_resp.status_code == 409
