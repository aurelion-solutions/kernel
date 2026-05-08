# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ORM-level tests for CapabilityMapping model — tests the DB CHECK constraint directly."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.inventory.access_model.capabilities.models import Capability
from src.inventory.access_model.capability_mappings.models import CapabilityMapping
from src.inventory.access_model.capability_scope_keys.models import CapabilityScopeKey


async def _seed_capability(session) -> int:
    cap = Capability(slug=f'cap-{uuid.uuid4().hex[:8]}', name='Test Capability')
    session.add(cap)
    await session.flush()
    return cap.id


async def _seed_scope_key(session) -> int:
    sk = CapabilityScopeKey(code=f'SK-{uuid.uuid4().hex[:8]}', name='Test Scope Key')
    session.add(sk)
    await session.flush()
    return sk.id


@pytest.mark.asyncio
async def test_create_mapping_with_resource_id_persists(session_factory) -> None:
    """Insert a mapping with resource_id set; fetch back and assert all columns."""
    resource_id = uuid.uuid4()
    async with session_factory() as session:
        # Create application and resource first
        from src.inventory.resources.models import Resource
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

        resource = Resource(
            id=resource_id,
            external_id=f'ext-{uuid.uuid4().hex[:8]}',
            application_id=app.id,
            kind='role',
            resource_type='role',
            resource_key=f'key-{uuid.uuid4().hex[:8]}',
        )
        session.add(resource)
        await session.flush()

        cap_id = await _seed_capability(session)
        sk_id = await _seed_scope_key(session)

        mapping = CapabilityMapping(
            capability_id=cap_id,
            resource_id=resource_id,
            resource_kind=None,
            resource_path_glob=None,
            scope_key_id=sk_id,
            scope_value_source={'kind': 'constant', 'value': 'test'},
        )
        session.add(mapping)
        await session.flush()
        await session.refresh(mapping)

        assert mapping.id > 0
        assert mapping.resource_id == resource_id
        assert mapping.resource_kind is None
        assert mapping.resource_path_glob is None
        assert mapping.is_active is True
        assert mapping.created_at is not None

        await session.commit()


@pytest.mark.asyncio
async def test_resource_match_xor_check_rejects_two_set(session_factory) -> None:
    """INSERT with both resource_id and resource_kind set must raise IntegrityError (CHECK violation)."""
    resource_id = uuid.uuid4()
    async with session_factory() as session:
        from src.inventory.resources.models import Resource
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

        resource = Resource(
            id=resource_id,
            external_id=f'ext-{uuid.uuid4().hex[:8]}',
            application_id=app.id,
            kind='role',
            resource_type='role',
            resource_key=f'key-{uuid.uuid4().hex[:8]}',
        )
        session.add(resource)
        await session.flush()

        cap_id = await _seed_capability(session)
        sk_id = await _seed_scope_key(session)

        with pytest.raises(IntegrityError):
            await session.execute(
                sa.insert(CapabilityMapping).values(
                    capability_id=cap_id,
                    resource_id=resource_id,
                    resource_kind='role',  # both set — CHECK must reject
                    resource_path_glob=None,
                    scope_key_id=sk_id,
                    scope_value_source={'kind': 'constant', 'value': 'x'},
                    is_active=True,
                )
            )


@pytest.mark.asyncio
async def test_resource_match_xor_check_rejects_zero_set(session_factory) -> None:
    """INSERT with all three resource-match columns NULL must raise IntegrityError (CHECK violation)."""
    async with session_factory() as session:
        cap_id = await _seed_capability(session)
        sk_id = await _seed_scope_key(session)

        with pytest.raises(IntegrityError):
            await session.execute(
                sa.insert(CapabilityMapping).values(
                    capability_id=cap_id,
                    resource_id=None,
                    resource_kind=None,
                    resource_path_glob=None,
                    scope_key_id=sk_id,
                    scope_value_source={'kind': 'constant', 'value': 'x'},
                    is_active=True,
                )
            )
