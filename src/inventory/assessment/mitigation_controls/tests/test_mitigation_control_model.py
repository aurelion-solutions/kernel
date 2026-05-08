# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Model tests for the MitigationControl ORM."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from src.inventory.assessment.mitigation_controls.models import (
    MitigationControl,
    MitigationControlType,
)


@pytest.mark.asyncio
async def test_create_mitigation_control_persists_all_fields(session_factory) -> None:
    """Inserting a MitigationControl persists all columns including defaults."""
    async with session_factory() as session:
        ctrl = MitigationControl(
            code='QUARTERLY_ATTESTATION',
            name='Quarterly access attestation',
            description='Periodic attestation.',
            type=MitigationControlType.attestation,
            is_active=True,
            created_by='alice@example.com',
        )
        session.add(ctrl)
        await session.flush()
        ctrl_id = ctrl.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(MitigationControl, ctrl_id)
        assert fetched is not None
        assert fetched.code == 'QUARTERLY_ATTESTATION'
        assert fetched.name == 'Quarterly access attestation'
        assert fetched.description == 'Periodic attestation.'
        assert fetched.type == MitigationControlType.attestation
        assert fetched.is_active is True
        assert fetched.created_by == 'alice@example.com'
        assert fetched.created_at is not None
        assert fetched.id == ctrl_id


@pytest.mark.asyncio
async def test_mitigation_control_defaults_after_flush(session_factory) -> None:
    """is_active defaults to True and created_at is populated after flush."""
    async with session_factory() as session:
        ctrl = MitigationControl(
            code='DUAL_APPROVAL',
            name='Dual approval',
            type=MitigationControlType.dual_approval,
        )
        session.add(ctrl)
        await session.flush()
        await session.refresh(ctrl)
        assert ctrl.is_active is True
        assert ctrl.created_at is not None
        assert ctrl.id is not None


@pytest.mark.asyncio
async def test_mitigation_control_code_is_unique(session_factory) -> None:
    """Inserting two controls with the same code raises IntegrityError."""
    async with session_factory() as session:
        ctrl1 = MitigationControl(
            code='SIEM_ALERTING',
            name='SIEM alerting',
            type=MitigationControlType.logging_alerting,
        )
        session.add(ctrl1)
        await session.flush()

        ctrl2 = MitigationControl(
            code='SIEM_ALERTING',
            name='SIEM alerting duplicate',
            type=MitigationControlType.logging_alerting,
        )
        session.add(ctrl2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_all_mitigation_control_type_values_accepted(session_factory) -> None:
    """Every MitigationControlType enum value can be stored without error."""
    for i, ctrl_type in enumerate(MitigationControlType):
        async with session_factory() as session:
            ctrl = MitigationControl(
                code=f'TEST_TYPE_{i}',
                name=f'Test {ctrl_type.value}',
                type=ctrl_type,
            )
            session.add(ctrl)
            await session.flush()
            assert ctrl.type == ctrl_type


@pytest.mark.asyncio
async def test_unknown_type_value_raises_error(session_factory) -> None:
    """Inserting an unknown string into type column via raw SQL raises DataError or IntegrityError."""
    async with session_factory() as session:
        with pytest.raises((DataError, IntegrityError, DBAPIError)):
            await session.execute(
                sa.text(
                    'INSERT INTO mitigation_controls (code, name, type, is_active)'
                    " VALUES ('BAD_TYPE', 'Bad Type', 'nonexistent_type', true)"
                )
            )
            await session.flush()
