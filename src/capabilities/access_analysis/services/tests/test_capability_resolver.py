# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for CapabilityResolverService.

Each test seeds Capability + CapabilityScopeKey + Application + Resource +
CapabilityMapping rows. No EffectiveGrant rows are seeded — the resolver never
reads effective_grants.

After each test we assert no CapabilityGrant rows were created (no-persistence
contract).
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.services import EffectiveGrantRef
from src.capabilities.access_analysis.services.capability_resolver import CapabilityResolverService
from src.inventory.resources.models import Resource
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_app(session) -> UUID:  # type: ignore[no-untyped-def]
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


async def _seed_resource(session, application_id: UUID, kind: str = 'role', external_id: str | None = None) -> UUID:  # type: ignore[no-untyped-def]
    ext = external_id or f'ext-{uuid.uuid4().hex[:8]}'
    resource = Resource(
        external_id=ext,
        application_id=application_id,
        kind=kind,
        resource_type=kind,
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _seed_capability(session, slug: str | None = None, is_active: bool = True) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    slug = slug or f'cap-{uuid.uuid4().hex[:8]}'
    cap = Capability(slug=slug, name=f'Capability {slug}', is_active=is_active)
    session.add(cap)
    await session.flush()
    return cap.id, slug


async def _seed_scope_key(session) -> int:  # type: ignore[no-untyped-def]
    sk = CapabilityScopeKey(code=f'SK-{uuid.uuid4().hex[:8]}', name='Test scope key')
    session.add(sk)
    await session.flush()
    return sk.id


async def _seed_mapping(  # type: ignore[no-untyped-def]
    session,
    *,
    capability_id: int,
    scope_key_id: int,
    resource_id: UUID | None = None,
    resource_kind: str | None = None,
    resource_path_glob: str | None = None,
    action_slug: str | None = None,
    application_id: UUID | None = None,
    is_active: bool = True,
) -> int:
    m = CapabilityMapping(
        capability_id=capability_id,
        scope_key_id=scope_key_id,
        scope_value_source={'kind': 'constant', 'value': 'test'},
        resource_id=resource_id,
        resource_kind=resource_kind,
        resource_path_glob=resource_path_glob,
        action_slug=action_slug,
        application_id=application_id,
        is_active=is_active,
    )
    session.add(m)
    await session.flush()
    return m.id


async def _assert_no_grants(session) -> None:  # type: ignore[no-untyped-def]
    count = (await session.execute(sa.select(sa.func.count()).select_from(CapabilityGrant))).scalar_one()
    assert count == 0, f'Expected 0 CapabilityGrant rows, found {count} — resolver wrote to DB!'


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_sources_returns_empty_list(session_factory) -> None:
    """sources=[] → [] without touching any DB table."""
    async with session_factory() as session:
        service = CapabilityResolverService(session)
        result = await service.resolve_capabilities_for_sources(sources=[])
        assert result == []
        await _assert_no_grants(session)


@pytest.mark.asyncio
async def test_resource_id_mapping_resolves_to_slug(session_factory) -> None:
    """Mapping by resource_id+action → correct slug returned."""
    async with session_factory() as session:
        app_id = await _seed_app(session)
        resource_id = await _seed_resource(session, app_id)
        cap_id, cap_slug = await _seed_capability(session, slug='create_vendor')
        sk_id = await _seed_scope_key(session)
        await _seed_mapping(
            session,
            capability_id=cap_id,
            scope_key_id=sk_id,
            resource_id=resource_id,
            action_slug='write',
        )
        await session.commit()

    async with session_factory() as session:
        service = CapabilityResolverService(session)
        sources = [
            EffectiveGrantRef(
                application_id=app_id,
                resource_id=resource_id,
                action_slug='write',
                resource_kind='role',
                resource_external_id='any-ext',
            )
        ]
        result = await service.resolve_capabilities_for_sources(sources=sources)
        assert result == ['create_vendor']
        await _assert_no_grants(session)


@pytest.mark.asyncio
async def test_distinct_slugs_when_multiple_mappings_match_same_slug(session_factory) -> None:
    """Two mappings pointing at the same capability_id → slug appears exactly once."""
    async with session_factory() as session:
        app_id = await _seed_app(session)
        resource_id = await _seed_resource(session, app_id, kind='report')
        cap_id, cap_slug = await _seed_capability(session, slug='run_report')
        sk_id = await _seed_scope_key(session)
        # Mapping 1: by resource_id
        await _seed_mapping(
            session,
            capability_id=cap_id,
            scope_key_id=sk_id,
            resource_id=resource_id,
        )
        # Mapping 2: by resource_kind
        await _seed_mapping(
            session,
            capability_id=cap_id,
            scope_key_id=sk_id,
            resource_kind='report',
        )
        await session.commit()

    async with session_factory() as session:
        service = CapabilityResolverService(session)
        sources = [
            EffectiveGrantRef(
                application_id=app_id,
                resource_id=resource_id,
                action_slug='read',
                resource_kind='report',
                resource_external_id='ext-report',
            )
        ]
        result = await service.resolve_capabilities_for_sources(sources=sources)
        assert result == ['run_report']
        assert result.count('run_report') == 1
        await _assert_no_grants(session)


@pytest.mark.asyncio
async def test_alphabetical_order_in_output(session_factory) -> None:
    """Multiple matched slugs → output is sorted alphabetically."""
    async with session_factory() as session:
        app_id = await _seed_app(session)
        resource_id = await _seed_resource(session, app_id, kind='invoice')
        sk_id = await _seed_scope_key(session)

        for slug in ['zeta_cap', 'alpha_cap', 'mu_cap']:
            cap_id, _ = await _seed_capability(session, slug=slug)
            await _seed_mapping(
                session,
                capability_id=cap_id,
                scope_key_id=sk_id,
                resource_kind='invoice',
            )
        await session.commit()

    async with session_factory() as session:
        service = CapabilityResolverService(session)
        sources = [
            EffectiveGrantRef(
                application_id=app_id,
                resource_id=resource_id,
                action_slug='read',
                resource_kind='invoice',
                resource_external_id='inv-001',
            )
        ]
        result = await service.resolve_capabilities_for_sources(sources=sources)
        assert result == sorted(result)
        assert set(result) == {'alpha_cap', 'mu_cap', 'zeta_cap'}
        await _assert_no_grants(session)


@pytest.mark.asyncio
async def test_inactive_mapping_is_ignored(session_factory) -> None:
    """Mapping with is_active=False → not included in output."""
    async with session_factory() as session:
        app_id = await _seed_app(session)
        resource_id = await _seed_resource(session, app_id, kind='ledger')
        cap_id, cap_slug = await _seed_capability(session, slug='post_journal')
        sk_id = await _seed_scope_key(session)
        await _seed_mapping(
            session,
            capability_id=cap_id,
            scope_key_id=sk_id,
            resource_kind='ledger',
            is_active=False,
        )
        await session.commit()

    async with session_factory() as session:
        service = CapabilityResolverService(session)
        sources = [
            EffectiveGrantRef(
                application_id=app_id,
                resource_id=resource_id,
                action_slug='write',
                resource_kind='ledger',
                resource_external_id='led-001',
            )
        ]
        result = await service.resolve_capabilities_for_sources(sources=sources)
        assert result == []
        await _assert_no_grants(session)


@pytest.mark.asyncio
async def test_inactive_capability_is_dropped_from_output(session_factory) -> None:
    """Active mapping matches, but target Capability.is_active=False → slug omitted."""
    async with session_factory() as session:
        app_id = await _seed_app(session)
        resource_id = await _seed_resource(session, app_id, kind='payment')
        cap_id, cap_slug = await _seed_capability(session, slug='approve_payment_inactive', is_active=False)
        sk_id = await _seed_scope_key(session)
        await _seed_mapping(
            session,
            capability_id=cap_id,
            scope_key_id=sk_id,
            resource_kind='payment',
            is_active=True,
        )
        await session.commit()

    async with session_factory() as session:
        service = CapabilityResolverService(session)
        sources = [
            EffectiveGrantRef(
                application_id=app_id,
                resource_id=resource_id,
                action_slug='write',
                resource_kind='payment',
                resource_external_id='pay-001',
            )
        ]
        result = await service.resolve_capabilities_for_sources(sources=sources)
        assert result == []
        await _assert_no_grants(session)


@pytest.mark.asyncio
async def test_hypothetical_source_with_no_db_eg_resolves(session_factory) -> None:
    """Source has no EffectiveGrant row — resolver bypasses EAS and still returns slug."""
    async with session_factory() as session:
        app_id = await _seed_app(session)
        resource_id = await _seed_resource(session, app_id, kind='vendor')
        cap_id, cap_slug = await _seed_capability(session, slug='create_vendor_hyp')
        sk_id = await _seed_scope_key(session)
        await _seed_mapping(
            session,
            capability_id=cap_id,
            scope_key_id=sk_id,
            resource_id=resource_id,
            action_slug='write',
        )
        await session.commit()
        # Deliberately NOT seeding any EffectiveGrant row

    async with session_factory() as session:
        service = CapabilityResolverService(session)
        sources = [
            EffectiveGrantRef(
                application_id=app_id,
                resource_id=resource_id,
                action_slug='write',
                resource_kind='vendor',
                resource_external_id='vend-ext',
            )
        ]
        result = await service.resolve_capabilities_for_sources(sources=sources)
        assert result == ['create_vendor_hyp']
        await _assert_no_grants(session)


@pytest.mark.asyncio
async def test_no_matching_mapping_returns_empty_list(session_factory) -> None:
    """Source whose action/resource combo no mapping covers → []."""
    async with session_factory() as session:
        app_id = await _seed_app(session)
        resource_id = await _seed_resource(session, app_id, kind='account')
        cap_id, _ = await _seed_capability(session, slug='delete_account')
        sk_id = await _seed_scope_key(session)
        # Mapping is for action_slug='delete', but source uses 'read'
        await _seed_mapping(
            session,
            capability_id=cap_id,
            scope_key_id=sk_id,
            resource_kind='account',
            action_slug='delete',
        )
        await session.commit()

    async with session_factory() as session:
        service = CapabilityResolverService(session)
        sources = [
            EffectiveGrantRef(
                application_id=app_id,
                resource_id=resource_id,
                action_slug='read',
                resource_kind='account',
                resource_external_id='acc-001',
            )
        ]
        result = await service.resolve_capabilities_for_sources(sources=sources)
        assert result == []
        await _assert_no_grants(session)
