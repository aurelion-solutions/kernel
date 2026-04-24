# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for POST /api/v0/sod/resolve-capabilities."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.inventory.resources.models import Resource
from src.platform.applications.models import Application

_URL = '/api/v0/sod/resolve-capabilities'


async def _seed_prereqs(session):  # type: ignore[no-untyped-def]
    """Seed app, resource, capability, scope_key, mapping. Returns (app_id, resource_id, slug)."""
    app = Application(
        name=f'app-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    resource = Resource(
        external_id=f'ext-{uuid.uuid4().hex[:8]}',
        application_id=app.id,
        kind='role',
        resource_type='role',
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
    )
    session.add(resource)
    await session.flush()

    slug = f'cap-{uuid.uuid4().hex[:8]}'
    cap = Capability(slug=slug, name=f'Capability {slug}')
    session.add(cap)
    await session.flush()

    sk = CapabilityScopeKey(code=f'SK-{uuid.uuid4().hex[:8]}', name='Test scope key')
    session.add(sk)
    await session.flush()

    mapping = CapabilityMapping(
        capability_id=cap.id,
        scope_key_id=sk.id,
        scope_value_source={'kind': 'constant', 'value': 'x'},
        resource_id=resource.id,
        action_slug='write',
        is_active=True,
    )
    session.add(mapping)
    await session.flush()

    return app.id, resource.id, slug


@pytest.mark.asyncio
async def test_post_resolve_capabilities_empty_sources_returns_200_empty_list(client) -> None:
    """POST with empty sources → 200, capability_slugs=[]."""
    response = await client.post(_URL, json={'sources': []})
    assert response.status_code == 200
    data = response.json()
    assert data == {'capability_slugs': []}


@pytest.mark.asyncio
async def test_post_resolve_capabilities_returns_distinct_sorted_slugs(client, session_factory) -> None:
    """Seed mappings; POST matching sources → 200, sorted distinct slugs."""
    async with session_factory() as session:
        app_id, resource_id, slug = await _seed_prereqs(session)
        await session.commit()

    payload = {
        'sources': [
            {
                'application_id': str(app_id),
                'resource_id': str(resource_id),
                'action_slug': 'write',
                'resource_kind': 'role',
                'resource_external_id': 'any',
            }
        ]
    }
    response = await client.post(_URL, json=payload)
    assert response.status_code == 200
    data = response.json()
    slugs = data['capability_slugs']
    assert slug in slugs
    assert slugs == sorted(slugs)
    assert len(slugs) == len(set(slugs))


@pytest.mark.asyncio
async def test_post_resolve_capabilities_validates_request_shape(client) -> None:
    """Malformed body (missing application_id) → 422 validation error."""
    payload = {
        'sources': [
            {
                # application_id is missing
                'resource_id': str(uuid.uuid4()),
                'action_slug': 'write',
                'resource_kind': 'role',
                'resource_external_id': 'any',
            }
        ]
    }
    response = await client.post(_URL, json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_resolve_capabilities_does_not_create_capability_grants(client, session_factory) -> None:
    """POST any valid body → capability_grants table stays empty."""
    async with session_factory() as session:
        app_id, resource_id, _ = await _seed_prereqs(session)
        await session.commit()

    payload = {
        'sources': [
            {
                'application_id': str(app_id),
                'resource_id': str(resource_id),
                'action_slug': 'write',
                'resource_kind': 'role',
                'resource_external_id': 'any',
            }
        ]
    }
    response = await client.post(_URL, json=payload)
    assert response.status_code == 200

    async with session_factory() as session:
        count = (await session.execute(sa.select(sa.func.count()).select_from(CapabilityGrant))).scalar_one()
        assert count == 0, f'Expected 0 CapabilityGrant rows, found {count}'
