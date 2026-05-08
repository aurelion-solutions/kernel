# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for CapabilityScopeKeyService."""

from __future__ import annotations

import pytest
from src.inventory.access_model.capability_scope_keys.exceptions import (
    CapabilityScopeKeyCodeAlreadyExistsError,
    CapabilityScopeKeyNotFoundError,
)
from src.inventory.access_model.capability_scope_keys.schemas import (
    CapabilityScopeKeyCreate,
    CapabilityScopeKeyPatch,
    CapabilityScopeKeyRead,
)
from src.inventory.access_model.capability_scope_keys.service import CapabilityScopeKeyService
from src.platform.logs.service import NoOpLogService


def _make_service(session) -> CapabilityScopeKeyService:
    return CapabilityScopeKeyService(session, NoOpLogService())


def _make_payload(code: str = 'GLOBAL', name: str = 'Global') -> CapabilityScopeKeyCreate:
    return CapabilityScopeKeyCreate(code=code, name=name)


@pytest.mark.asyncio
async def test_create_returns_scope_key_read_with_generated_id(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        result = await service.create(_make_payload())
        assert isinstance(result, CapabilityScopeKeyRead)
        assert result.id > 0
        assert result.code == 'GLOBAL'
        assert result.name == 'Global'
        assert result.is_active is True
        assert result.created_at is not None


@pytest.mark.asyncio
async def test_create_duplicate_code_raises_capability_scope_key_code_already_exists_error(session_factory) -> None:
    # First session — create the original entry and commit so it persists.
    async with session_factory() as session:
        service = _make_service(session)
        await service.create(_make_payload(code='LEGAL_ENTITY', name='Legal entity'))
        await session.commit()

    # Second session — attempt to create with the same code; expect domain error.
    with pytest.raises(CapabilityScopeKeyCodeAlreadyExistsError) as exc_info:
        async with session_factory() as session:
            service = _make_service(session)
            await service.create(_make_payload(code='LEGAL_ENTITY', name='Legal entity again'))
    assert exc_info.value.code == 'LEGAL_ENTITY'


@pytest.mark.asyncio
async def test_get_missing_id_raises_capability_scope_key_not_found_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        with pytest.raises(CapabilityScopeKeyNotFoundError) as exc_info:
            await service.get(99999)
    assert exc_info.value.scope_key_id == 99999


@pytest.mark.asyncio
async def test_patch_updates_only_provided_fields_and_does_not_touch_code(session_factory) -> None:
    """PATCH with name only must update name, leave code and is_active unchanged."""
    async with session_factory() as session:
        service = _make_service(session)
        created = await service.create(_make_payload(code='DEPARTMENT', name='Original Name'))
        original_code = created.code

        patched = await service.patch(created.id, CapabilityScopeKeyPatch(name='New name'))
        assert patched.name == 'New name'
        assert patched.code == original_code
        assert patched.is_active == created.is_active


@pytest.mark.asyncio
async def test_patch_missing_id_raises_capability_scope_key_not_found_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        with pytest.raises(CapabilityScopeKeyNotFoundError):
            await service.patch(99999, CapabilityScopeKeyPatch(name='Whatever'))


@pytest.mark.asyncio
async def test_deactivate_sets_is_active_false_and_is_idempotent(session_factory) -> None:
    """deactivate sets is_active=False; calling twice still returns False without error."""
    async with session_factory() as session:
        service = _make_service(session)
        created = await service.create(_make_payload(code='REGION', name='Region'))
        assert created.is_active is True

        result1 = await service.deactivate(created.id)
        assert result1.is_active is False

        # second call — idempotent
        result2 = await service.deactivate(created.id)
        assert result2.is_active is False


@pytest.mark.asyncio
async def test_list_filters_by_is_active(session_factory) -> None:
    """list() with is_active=True/False filters correctly; without filter returns all."""
    async with session_factory() as session:
        service = _make_service(session)
        await service.create(_make_payload(code='PROJECT', name='Project'))
        await service.create(_make_payload(code='PROGRAM', name='Program'))
        inactive_payload = CapabilityScopeKeyCreate(code='TENANT', name='Tenant', is_active=False)
        await service.create(inactive_payload)

        active_list = await service.list(is_active=True)
        assert len(active_list) == 2
        assert all(sk.is_active for sk in active_list)

        inactive_list = await service.list(is_active=False)
        assert len(inactive_list) == 1
        assert inactive_list[0].code == 'TENANT'

        all_list = await service.list()
        assert len(all_list) == 3
