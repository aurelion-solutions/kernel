# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Model tests for the Mitigation ORM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.inventory.assessment.mitigation_controls.models import MitigationControl, MitigationControlType
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus
from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_rule(session) -> SodRule:
    rule = SodRule(
        code=f'RULE-{uuid.uuid4().hex[:6]}',
        name='Test Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
        mitigation_allowed=True,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def _insert_control(session) -> MitigationControl:
    ctrl = MitigationControl(
        code=f'CTRL-{uuid.uuid4().hex[:6]}',
        name='Test Control',
        type=MitigationControlType.attestation,
        is_active=True,
    )
    session.add(ctrl)
    await session.flush()
    await session.refresh(ctrl)
    return ctrl


async def _insert_subject(session) -> uuid.UUID:
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    nhi = await create_nhi(
        session,
        external_id=f'tst-nhi-{uuid.uuid4().hex[:8]}',
        name='Test NHI',
        kind='service_account',
    )
    subject = await create_subject(
        session,
        external_id=f'tst-subj-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status=SubjectNHIStatus.active,
    )
    return subject.id


def _now() -> datetime:
    return datetime.now(UTC)


def _mitigation(
    rule: SodRule, ctrl: MitigationControl, subject_id: uuid.UUID, owner_id: uuid.UUID, **kwargs
) -> Mitigation:
    defaults = {
        'rule_id': rule.id,
        'control_id': ctrl.id,
        'subject_id': subject_id,
        'owner_id': owner_id,
        'valid_from': _now(),
        'status': MitigationStatus.proposed,
        'scope_key_id': None,
        'scope_value': None,
    }
    defaults.update(kwargs)
    return Mitigation(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mitigation_persists_all_fields(session_factory) -> None:
    """Inserting a Mitigation persists all columns; defaults materialised after flush."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)

        m = _mitigation(rule, ctrl, subject_id, owner_id, created_by='alice@example.com')
        session.add(m)
        await session.flush()
        await session.refresh(m)

        assert m.id is not None
        assert m.status == MitigationStatus.proposed
        assert m.created_at is not None
        assert m.created_by == 'alice@example.com'
        assert m.scope_key_id is None
        assert m.scope_value is None


@pytest.mark.asyncio
async def test_duplicate_proposed_same_scope_raises_integrity_error(session_factory) -> None:
    """Duplicate (rule, subject, None, None) both proposed → IntegrityError (partial unique)."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)

        m1 = _mitigation(rule, ctrl, subject_id, owner_id)
        session.add(m1)
        await session.flush()

        m2 = _mitigation(rule, ctrl, subject_id, owner_id)
        session.add(m2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_revoked_then_new_proposed_same_scope_succeeds(session_factory) -> None:
    """After revoking, a new proposed for same scope is allowed (revoked excluded from partial unique)."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)

        m1 = _mitigation(rule, ctrl, subject_id, owner_id, status=MitigationStatus.revoked)
        session.add(m1)
        await session.flush()

        m2 = _mitigation(rule, ctrl, subject_id, owner_id, status=MitigationStatus.proposed)
        session.add(m2)
        await session.flush()
        await session.refresh(m2)

        assert m2.id is not None
        assert m2.status == MitigationStatus.proposed


@pytest.mark.asyncio
async def test_two_unscoped_both_proposed_raises_integrity_error(session_factory) -> None:
    """NULLS NOT DISTINCT: two unscoped proposed rows for same (rule, subject) → IntegrityError."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)

        m1 = _mitigation(rule, ctrl, subject_id, owner_id, status=MitigationStatus.proposed)
        session.add(m1)
        await session.flush()

        m2 = _mitigation(rule, ctrl, subject_id, owner_id, status=MitigationStatus.proposed)
        session.add(m2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_valid_until_not_after_valid_from_raises_check_violation(session_factory) -> None:
    """valid_until <= valid_from → IntegrityError (ck_mitigations_valid_window)."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)

        now = _now()
        m = _mitigation(
            rule,
            ctrl,
            subject_id,
            owner_id,
            valid_from=now,
            valid_until=now - timedelta(seconds=1),
        )
        session.add(m)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_scope_key_without_scope_value_raises_check_violation(session_factory) -> None:
    """scope_key_id set without scope_value → IntegrityError (ck_mitigations_scope_pair)."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)

        # Insert scope_key_id without scope_value via raw SQL to bypass ORM
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text(
                    'INSERT INTO mitigations '
                    '(rule_id, control_id, subject_id, owner_id, valid_from, status, scope_key_id, scope_value) '
                    'VALUES (:rule_id, :control_id, :subject_id, :owner_id, :valid_from, :status, 999, NULL)'
                ),
                {
                    'rule_id': rule.id,
                    'control_id': ctrl.id,
                    'subject_id': str(subject_id),
                    'owner_id': str(owner_id),
                    'valid_from': _now(),
                    'status': 'proposed',
                },
            )
            await session.flush()


@pytest.mark.asyncio
async def test_every_mitigation_status_value_accepted(session_factory) -> None:
    """Every MitigationStatus value is accepted by raw insert."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        owner_id = await _insert_subject(session)

        for status in MitigationStatus:
            subject_id = await _insert_subject(session)
            m = _mitigation(rule, ctrl, subject_id, owner_id, status=status)
            session.add(m)
            await session.flush()
            assert m.status == status
