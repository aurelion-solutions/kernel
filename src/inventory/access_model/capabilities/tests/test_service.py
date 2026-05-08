# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for CapabilityService."""

from __future__ import annotations

import pytest
from src.inventory.access_model.capabilities.exceptions import (
    CapabilityNotFoundError,
    CapabilitySlugAlreadyExistsError,
)
from src.inventory.access_model.capabilities.schemas import (
    CapabilityCreate,
    CapabilityPatch,
    CapabilityRead,
)
from src.inventory.access_model.capabilities.service import CapabilityService
from src.platform.logs.service import NoOpLogService


def _make_service(session) -> CapabilityService:
    return CapabilityService(session, NoOpLogService())


def _make_payload(slug: str = 'approve_payment', name: str = 'Approve Payment') -> CapabilityCreate:
    return CapabilityCreate(slug=slug, name=name)


@pytest.mark.asyncio
async def test_create_returns_capability_read_with_generated_id(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        result = await service.create(_make_payload())
        assert isinstance(result, CapabilityRead)
        assert result.id > 0
        assert result.slug == 'approve_payment'
        assert result.name == 'Approve Payment'
        assert result.is_active is True
        assert result.created_at is not None


@pytest.mark.asyncio
async def test_create_duplicate_slug_raises_capability_slug_already_exists_error(session_factory) -> None:
    # First session — create the original entry and commit so it persists.
    async with session_factory() as session:
        service = _make_service(session)
        await service.create(_make_payload(slug='create_vendor', name='Create Vendor'))
        await session.commit()

    # Second session — attempt to create with the same slug; expect domain error.
    with pytest.raises(CapabilitySlugAlreadyExistsError) as exc_info:
        async with session_factory() as session:
            service = _make_service(session)
            await service.create(_make_payload(slug='create_vendor', name='Create Vendor Again'))
    assert exc_info.value.slug == 'create_vendor'


@pytest.mark.asyncio
async def test_get_missing_id_raises_capability_not_found_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        with pytest.raises(CapabilityNotFoundError) as exc_info:
            await service.get(99999)
    assert exc_info.value.capability_id == 99999


@pytest.mark.asyncio
async def test_patch_updates_only_provided_fields_and_does_not_touch_slug(session_factory) -> None:
    """PATCH with name only must update name, leave slug and is_active unchanged."""
    async with session_factory() as session:
        service = _make_service(session)
        created = await service.create(_make_payload(slug='post_journal', name='Original Name'))
        original_slug = created.slug

        patched = await service.patch(created.id, CapabilityPatch(name='New Name'))
        assert patched.name == 'New Name'
        assert patched.slug == original_slug
        assert patched.is_active == created.is_active


@pytest.mark.asyncio
async def test_patch_missing_id_raises_capability_not_found_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        with pytest.raises(CapabilityNotFoundError):
            await service.patch(99999, CapabilityPatch(name='Whatever'))


@pytest.mark.asyncio
async def test_deactivate_sets_is_active_false_and_is_idempotent(session_factory) -> None:
    """deactivate sets is_active=False; calling twice still returns False without error."""
    async with session_factory() as session:
        service = _make_service(session)
        created = await service.create(_make_payload(slug='risky_op', name='Risky Op'))
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
        await service.create(_make_payload(slug='cap_active_1', name='Active 1'))
        await service.create(_make_payload(slug='cap_active_2', name='Active 2'))
        inactive_payload = CapabilityCreate(slug='cap_inactive_1', name='Inactive 1', is_active=False)
        await service.create(inactive_payload)

        active_list = await service.list(is_active=True)
        assert len(active_list) == 2
        assert all(c.is_active for c in active_list)

        inactive_list = await service.list(is_active=False)
        assert len(inactive_list) == 1
        assert inactive_list[0].slug == 'cap_inactive_1'

        all_list = await service.list()
        assert len(all_list) == 3
