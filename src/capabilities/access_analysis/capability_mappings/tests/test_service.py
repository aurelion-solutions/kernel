# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for CapabilityMappingService."""

from __future__ import annotations

import uuid

import pytest
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_mappings.exceptions import (
    CapabilityMappingDefaultScopeKeyNotSeededError,
    CapabilityMappingNotFoundError,
    CapabilityMappingResourceMatchExclusivityError,
    CapabilityMappingUnknownActionSlugError,
    CapabilityMappingUnknownCapabilityIdError,
)
from src.capabilities.access_analysis.capability_mappings.schemas import CapabilityMappingCreate
from src.capabilities.access_analysis.capability_mappings.service import CapabilityMappingService
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.platform.logs.service import NoOpLogService


def _make_service(session) -> CapabilityMappingService:
    return CapabilityMappingService(session, NoOpLogService())


async def _seed_capability(session) -> int:
    cap = Capability(slug=f'cap-{uuid.uuid4().hex[:8]}', name='Test Capability')
    session.add(cap)
    await session.flush()
    return cap.id


async def _seed_global_scope_key(session) -> int:
    """Insert the GLOBAL scope key so _resolve_default_scope_key_id works."""
    sk = CapabilityScopeKey(code='GLOBAL', name='Global')
    session.add(sk)
    await session.flush()
    return sk.id


async def _seed_application(session):
    from src.platform.applications.models import Application

    app = Application(
        name=f'app-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _seed_resource(session, application_id):
    from src.inventory.resources.models import Resource

    resource = Resource(
        external_id=f'ext-{uuid.uuid4().hex[:8]}',
        application_id=application_id,
        kind='role',
        resource_type='role',
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
    )
    session.add(resource)
    await session.flush()
    return resource.id


def _make_create_payload(capability_id: int, scope_key_id: int | None = None, **overrides) -> CapabilityMappingCreate:
    defaults = {
        'capability_id': capability_id,
        'resource_kind': 'role',
        'scope_key_id': scope_key_id,
        'scope_value_source': {'kind': 'constant', 'value': 'admin'},
    }
    defaults.update(overrides)
    return CapabilityMappingCreate(**defaults)


@pytest.mark.asyncio
async def test_create_inserts_mapping_with_global_scope_default(session_factory) -> None:
    """scope_key_id=None resolves to the GLOBAL scope key id."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        global_sk_id = await _seed_global_scope_key(session)
        service = _make_service(session)

        payload = _make_create_payload(cap_id, scope_key_id=None)
        result = await service.create(payload)

        assert result.scope_key_id == global_sk_id
        assert result.capability_id == cap_id
        assert result.resource_kind == 'role'


@pytest.mark.asyncio
async def test_create_without_global_scope_key_raises_default_scope_key_not_seeded_error(session_factory) -> None:
    """If GLOBAL scope key is not seeded, raises CapabilityMappingDefaultScopeKeyNotSeededError."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        service = _make_service(session)

        payload = _make_create_payload(cap_id, scope_key_id=None)
        with pytest.raises(CapabilityMappingDefaultScopeKeyNotSeededError):
            await service.create(payload)


@pytest.mark.asyncio
async def test_create_with_unknown_action_slug_raises_unknown_action_slug_error(session_factory) -> None:
    """action_slug that doesn't exist in ref_actions raises CapabilityMappingUnknownActionSlugError."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        global_sk_id = await _seed_global_scope_key(session)
        service = _make_service(session)

        payload = _make_create_payload(cap_id, scope_key_id=global_sk_id, action_slug='nonexistent_slug')
        with pytest.raises(CapabilityMappingUnknownActionSlugError) as exc_info:
            await service.create(payload)
        assert exc_info.value.action_slug == 'nonexistent_slug'


@pytest.mark.asyncio
async def test_create_with_known_action_slug_succeeds(session_factory) -> None:
    """action_slug 'read' (seeded in conftest) should be accepted."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        global_sk_id = await _seed_global_scope_key(session)
        service = _make_service(session)

        payload = _make_create_payload(cap_id, scope_key_id=global_sk_id, action_slug='read')
        result = await service.create(payload)
        assert result.action_slug == 'read'


@pytest.mark.asyncio
async def test_create_with_unknown_capability_id_raises_unknown_capability_id_error(session_factory) -> None:
    """capability_id that doesn't exist raises CapabilityMappingUnknownCapabilityIdError (FK path)."""
    async with session_factory() as session:
        global_sk_id = await _seed_global_scope_key(session)
        service = _make_service(session)

        payload = _make_create_payload(999_999_999, scope_key_id=global_sk_id)
        with pytest.raises(CapabilityMappingUnknownCapabilityIdError) as exc_info:
            await service.create(payload)
        assert exc_info.value.capability_id == 999_999_999


@pytest.mark.asyncio
async def test_patch_resource_match_swap_succeeds(session_factory) -> None:
    """Create mapping with resource_id; patch swaps to resource_kind. XOR still satisfied."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        global_sk_id = await _seed_global_scope_key(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)

        service = _make_service(session)
        payload = _make_create_payload(
            cap_id,
            scope_key_id=global_sk_id,
            resource_kind=None,
            resource_id=resource_id,
        )
        created = await service.create(payload)
        assert created.resource_id == resource_id
        assert created.resource_kind is None

        # Patch: swap to resource_kind, clear resource_id
        patched = await service.patch(
            created.id,
            {'resource_id': None, 'resource_kind': 'account'},
        )
        assert patched.resource_id is None
        assert patched.resource_kind == 'account'


@pytest.mark.asyncio
async def test_patch_to_zero_resource_match_raises_resource_match_exclusivity_error(session_factory) -> None:
    """Patch that sets resource_id=None without setting an alternative raises XOR error."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        global_sk_id = await _seed_global_scope_key(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)

        service = _make_service(session)
        payload = _make_create_payload(
            cap_id,
            scope_key_id=global_sk_id,
            resource_kind=None,
            resource_id=resource_id,
        )
        created = await service.create(payload)

        with pytest.raises(CapabilityMappingResourceMatchExclusivityError):
            await service.patch(created.id, {'resource_id': None})


@pytest.mark.asyncio
async def test_delete_returns_none_and_removes_row(session_factory) -> None:
    """Create → delete → get raises NotFoundError."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        global_sk_id = await _seed_global_scope_key(session)
        service = _make_service(session)

        payload = _make_create_payload(cap_id, scope_key_id=global_sk_id)
        created = await service.create(payload)
        mapping_id = created.id

        await service.delete(mapping_id)

        with pytest.raises(CapabilityMappingNotFoundError):
            await service.get(mapping_id)


@pytest.mark.asyncio
async def test_get_missing_id_raises_capability_mapping_not_found_error(session_factory) -> None:
    async with session_factory() as session:
        service = _make_service(session)
        with pytest.raises(CapabilityMappingNotFoundError) as exc_info:
            await service.get(99999)
        assert exc_info.value.mapping_id == 99999


@pytest.mark.asyncio
async def test_delete_mapping_with_dependent_capability_grants_raises_in_use_error(session_factory) -> None:
    """Seed mapping; project one EG → one capability grant; delete → CapabilityMappingInUseError."""
    from datetime import UTC, datetime

    import sqlalchemy as sa
    from src.capabilities.access_analysis.capability_grants.service import CapabilityProjectionService
    from src.capabilities.access_analysis.capability_grants.tests import _seed_minimal_refs
    from src.capabilities.access_analysis.capability_mappings.exceptions import CapabilityMappingInUseError
    from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping

    now = datetime.now(UTC)

    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)
        mapping_id = refs.mapping_id

        # Project one EG to create a live capability grant
        proj_service = CapabilityProjectionService(session)
        await proj_service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=now)

        # Attempt to delete the mapping — should fail
        svc = _make_service(session)
        with pytest.raises(CapabilityMappingInUseError) as exc_info:
            await svc.delete(mapping_id)

        assert exc_info.value.mapping_id == mapping_id
        assert exc_info.value.grant_count == 1

        # Mapping row must still exist
        row = (
            (await session.execute(sa.select(CapabilityMapping).where(CapabilityMapping.id == mapping_id)))
            .scalars()
            .one_or_none()
        )
        assert row is not None


@pytest.mark.asyncio
async def test_list_filters_by_capability_id_and_is_active(session_factory) -> None:
    """Insert two active mappings for capability A and one inactive for B; verify filter matrices."""
    async with session_factory() as session:
        cap_a_id = await _seed_capability(session)
        cap_b_id = await _seed_capability(session)
        global_sk_id = await _seed_global_scope_key(session)
        service = _make_service(session)

        # Two active for cap_a
        await service.create(_make_create_payload(cap_a_id, scope_key_id=global_sk_id, resource_kind='role'))
        await service.create(_make_create_payload(cap_a_id, scope_key_id=global_sk_id, resource_kind='account'))
        # One inactive for cap_b
        await service.create(
            _make_create_payload(cap_b_id, scope_key_id=global_sk_id, resource_kind='group', is_active=False)
        )

        all_for_a = await service.list(capability_id=cap_a_id)
        assert len(all_for_a) == 2
        assert all(m.capability_id == cap_a_id for m in all_for_a)

        active_for_a = await service.list(capability_id=cap_a_id, is_active=True)
        assert len(active_for_a) == 2

        inactive = await service.list(is_active=False)
        assert len(inactive) == 1
        assert inactive[0].capability_id == cap_b_id
