# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for MitigationControlService."""

from __future__ import annotations

from pydantic import ValidationError
import pytest
from src.capabilities.access_analysis.mitigation_controls.exceptions import (
    MitigationControlCodeAlreadyExistsError,
    MitigationControlNotFoundError,
)
from src.capabilities.access_analysis.mitigation_controls.models import MitigationControlType
from src.capabilities.access_analysis.mitigation_controls.schemas import (
    MitigationControlCreate,
    MitigationControlPatch,
    MitigationControlRead,
)
from src.capabilities.access_analysis.mitigation_controls.service import MitigationControlService
from src.platform.logs.service import NoOpLogService


def _make_service(session) -> MitigationControlService:
    return MitigationControlService(session, NoOpLogService())


def _make_payload(
    code: str = 'QUARTERLY_ATTESTATION',
    name: str = 'Quarterly access attestation',
    ctrl_type: MitigationControlType = MitigationControlType.attestation,
) -> MitigationControlCreate:
    return MitigationControlCreate(code=code, name=name, type=ctrl_type)


@pytest.mark.asyncio
async def test_create_returns_mitigation_control_read_with_generated_id(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        result = await service.create(_make_payload())
        assert isinstance(result, MitigationControlRead)
        assert result.id > 0
        assert result.code == 'QUARTERLY_ATTESTATION'
        assert result.name == 'Quarterly access attestation'
        assert result.type == MitigationControlType.attestation
        assert result.is_active is True
        assert result.created_at is not None


@pytest.mark.asyncio
async def test_create_duplicate_code_raises_code_already_exists_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        await service.create(
            _make_payload(
                code='DUAL_APPROVAL',
                name='Dual Approval',
                ctrl_type=MitigationControlType.dual_approval,
            )
        )
        await session.commit()

    with pytest.raises(MitigationControlCodeAlreadyExistsError) as exc_info:
        async with session_factory() as session:
            service = _make_service(session)
            await service.create(
                _make_payload(
                    code='DUAL_APPROVAL',
                    name='Dual Approval Again',
                    ctrl_type=MitigationControlType.dual_approval,
                )
            )
    assert exc_info.value.code == 'DUAL_APPROVAL'


@pytest.mark.asyncio
async def test_list_no_filter_returns_all_rows(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        await service.create(_make_payload(code='MC_LIST_1', name='List 1'))
        await service.create(_make_payload(code='MC_LIST_2', name='List 2'))
        await service.create(_make_payload(code='MC_LIST_3', name='List 3'))

        rows = await service.list()
        codes = [r.code for r in rows]
        assert 'MC_LIST_1' in codes
        assert 'MC_LIST_2' in codes
        assert 'MC_LIST_3' in codes


@pytest.mark.asyncio
async def test_list_filter_by_is_active(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        c1 = await service.create(_make_payload(code='MC_ACTIVE_1', name='Active 1'))
        await service.create(_make_payload(code='MC_ACTIVE_2', name='Active 2'))
        # deactivate first
        await service.deactivate(c1.id)

        active_list = await service.list(is_active=True)
        active_codes = [r.code for r in active_list]
        assert 'MC_ACTIVE_1' not in active_codes
        assert 'MC_ACTIVE_2' in active_codes

        inactive_list = await service.list(is_active=False)
        inactive_codes = [r.code for r in inactive_list]
        assert 'MC_ACTIVE_1' in inactive_codes
        assert 'MC_ACTIVE_2' not in inactive_codes


@pytest.mark.asyncio
async def test_list_filter_by_type(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        await service.create(
            _make_payload(code='MC_ATTEST_1', name='Attest 1', ctrl_type=MitigationControlType.attestation)
        )
        await service.create(
            _make_payload(code='MC_DUAL_1', name='Dual 1', ctrl_type=MitigationControlType.dual_approval)
        )

        attest_list = await service.list(type=MitigationControlType.attestation)
        assert all(r.type == MitigationControlType.attestation for r in attest_list)
        attest_codes = [r.code for r in attest_list]
        assert 'MC_ATTEST_1' in attest_codes
        assert 'MC_DUAL_1' not in attest_codes


@pytest.mark.asyncio
async def test_list_pagination(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        await service.create(_make_payload(code='MC_PAGE_1', name='Page 1'))
        await service.create(_make_payload(code='MC_PAGE_2', name='Page 2'))
        await service.create(_make_payload(code='MC_PAGE_3', name='Page 3'))

        page1 = await service.list(limit=2, offset=0)
        page2 = await service.list(limit=2, offset=2)
        assert len(page1) == 2
        # offset=2 should return at least 1 row (could be more from other tests)
        assert len(page2) >= 1


@pytest.mark.asyncio
async def test_get_missing_id_raises_not_found_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        with pytest.raises(MitigationControlNotFoundError) as exc_info:
            await service.get(99999)
    assert exc_info.value.control_id == 99999


@pytest.mark.asyncio
async def test_patch_updates_provided_fields(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        created = await service.create(_make_payload(code='MC_PATCH_1', name='Original Name'))

        patched = await service.patch(
            created.id,
            MitigationControlPatch(
                name='Updated Name',
                description='New description',
                type=MitigationControlType.dual_approval,
            ),
        )
        assert patched.name == 'Updated Name'
        assert patched.description == 'New description'
        assert patched.type == MitigationControlType.dual_approval
        assert patched.code == 'MC_PATCH_1'  # code unchanged


@pytest.mark.asyncio
async def test_patch_schema_rejects_code_field() -> None:
    """MitigationControlPatch with extra='forbid' must reject 'code' field."""
    with pytest.raises(ValidationError):
        MitigationControlPatch.model_validate({'code': 'NEW_CODE', 'name': 'New Name'})


@pytest.mark.asyncio
async def test_deactivate_is_idempotent(session_factory) -> None:
    """deactivate sets is_active=False; calling twice still returns False without error."""
    async with session_factory() as session:
        service = _make_service(session)
        created = await service.create(_make_payload(code='MC_DEACT_1', name='To Deactivate'))
        assert created.is_active is True

        result1 = await service.deactivate(created.id)
        assert result1.is_active is False

        # second call — idempotent
        result2 = await service.deactivate(created.id)
        assert result2.is_active is False


@pytest.mark.asyncio
async def test_deactivate_missing_id_raises_not_found_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        with pytest.raises(MitigationControlNotFoundError):
            await service.deactivate(99999)
