# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ResourceService."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.resources.schemas import ResourcePatch
from src.inventory.resources.service import (
    DuplicateResourceAttributeError,
    DuplicateResourceError,
    ResourceApplicationNotFoundError,
    ResourceAttributeNotFoundError,
    ResourceNotFoundError,
    ResourceParentNotFoundError,
    ResourceService,
)
from src.platform.events.schemas import EventParticipantKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(event_service: EventService) -> ResourceService:
    return ResourceService(event_service=event_service)


async def _make_application_id(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


# ---------------------------------------------------------------------------
# Behavioural tests (rewritten — log assertions removed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_resource_happy_path(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource creates resource and returns it with correct fields."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-res-001',
            application_id=app_id,
            kind='database',
        )
        await session.commit()

    assert resource.id is not None
    assert resource.external_id == 'svc-res-001'
    assert resource.kind == 'database'


@pytest.mark.asyncio
async def test_create_resource_bad_application_id(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource raises ResourceApplicationNotFoundError for unknown application_id."""
    with pytest.raises(ResourceApplicationNotFoundError):
        async with session_factory() as session:
            await service.create_resource(
                session,
                external_id='svc-res-bad-app',
                application_id=uuid.uuid4(),
                kind='table',
            )


@pytest.mark.asyncio
async def test_create_resource_bad_parent_id(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource raises ResourceParentNotFoundError for unknown parent_id."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await session.commit()

    with pytest.raises(ResourceParentNotFoundError):
        async with session_factory() as session:
            await service.create_resource(
                session,
                external_id='svc-res-bad-parent',
                application_id=app_id,
                kind='file',
                parent_id=uuid.uuid4(),
            )


@pytest.mark.asyncio
async def test_create_resource_duplicate(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource raises DuplicateResourceError for same (application_id, external_id)."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.create_resource(
            session,
            external_id='svc-dup-001',
            application_id=app_id,
            kind='table',
        )
        await session.commit()

    with pytest.raises(DuplicateResourceError):
        async with session_factory() as session:
            await service.create_resource(
                session,
                external_id='svc-dup-001',
                application_id=app_id,
                kind='view',
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_resource_found(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource returns resource when found."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-get-001',
            application_id=app_id,
            kind='bucket',
        )
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        found = await service.get_resource(session, resource_id)

    assert found is not None
    assert found.id == resource_id


@pytest.mark.asyncio
async def test_get_resource_missing(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource returns None for unknown id."""
    async with session_factory() as session:
        result = await service.get_resource(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_update_resource_privilege_level(
    service: ResourceService,
    session_factory,
) -> None:
    """update_resource changes privilege_level."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-upd-001',
            application_id=app_id,
            kind='api',
        )
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        patch = ResourcePatch(privilege_level='admin')
        updated = await service.update_resource(session, resource_id, patch)
        await session.commit()

    assert updated.privilege_level.value == 'admin'


@pytest.mark.asyncio
async def test_update_resource_no_op_emits_nothing(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_resource with same value does not emit inventory.resource.updated.

    Replaces the log-era test_update_resource_no_op which counted log file lines.
    The two contracts are equivalent for this specific invariant: a no-op PATCH
    must produce zero side-effects regardless of which bus is used.
    """
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-noop-001',
            application_id=app_id,
            kind='table',
        )
        await session.commit()
        resource_id = resource.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = ResourcePatch(kind='table')
        await service.update_resource(session, resource_id, patch)
        await session.commit()

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_add_attribute_happy_path(
    service: ResourceService,
    session_factory,
) -> None:
    """add_attribute adds attribute with correct key and value."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-add-001',
            application_id=app_id,
            kind='storage',
        )
        await session.flush()
        attr = await service.add_attribute(session, resource.id, 'owner', 'alice')
        await session.commit()

    assert attr.key == 'owner'
    assert attr.value == 'alice'


@pytest.mark.asyncio
async def test_add_attribute_duplicate(
    service: ResourceService,
    session_factory,
) -> None:
    """add_attribute raises DuplicateResourceAttributeError on duplicate key."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-dup-001',
            application_id=app_id,
            kind='file',
        )
        await session.flush()
        await service.add_attribute(session, resource.id, 'tag', 'v1')
        await session.commit()
        resource_id = resource.id

    with pytest.raises(DuplicateResourceAttributeError):
        async with session_factory() as session:
            from src.inventory.resources.repository import get_resource_by_id

            res = await get_resource_by_id(session, resource_id)
            assert res is not None
            await service.add_attribute(session, res.id, 'tag', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_remove_attribute_happy_path(
    service: ResourceService,
    session_factory,
) -> None:
    """remove_attribute removes attribute; list_attributes returns empty afterwards."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-rm-001',
            application_id=app_id,
            kind='queue',
        )
        await session.flush()
        await service.add_attribute(session, resource.id, 'env', 'prod')
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        await service.remove_attribute(session, resource_id, 'env')
        await session.commit()

    async with session_factory() as session:
        attrs = await service.list_attributes(session, resource_id)
    assert attrs == []


@pytest.mark.asyncio
async def test_remove_attribute_missing(
    service: ResourceService,
    session_factory,
) -> None:
    """remove_attribute raises ResourceAttributeNotFoundError for missing key."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-rmm-001',
            application_id=app_id,
            kind='topic',
        )
        await session.commit()
        resource_id = resource.id

    with pytest.raises(ResourceAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, resource_id, 'nonexistent')


@pytest.mark.asyncio
async def test_list_attributes_not_found(
    service: ResourceService,
    session_factory,
) -> None:
    """list_attributes raises ResourceNotFoundError for unknown resource."""
    with pytest.raises(ResourceNotFoundError):
        async with session_factory() as session:
            await service.list_attributes(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_get_resource_by_external_id_returns_resource_when_present(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource_by_external_id returns the resource when (application_id, external_id) exists."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        created = await service.create_resource(
            session,
            external_id='ext-lookup-001',
            application_id=app_id,
            kind='folder',
        )
        await session.commit()
        resource_id = created.id

    async with session_factory() as session:
        found = await service.get_resource_by_external_id(
            session,
            application_id=app_id,
            external_id='ext-lookup-001',
        )

    assert found is not None
    assert found.id == resource_id
    assert found.external_id == 'ext-lookup-001'


@pytest.mark.asyncio
async def test_get_resource_by_external_id_returns_none_when_absent(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource_by_external_id returns None when the (application_id, external_id) does not exist."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await session.commit()

    async with session_factory() as session:
        found = await service.get_resource_by_external_id(
            session,
            application_id=app_id,
            external_id='nonexistent-external-id',
        )

    assert found is None


# ---------------------------------------------------------------------------
# Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_resource_emits_inventory_resource_created(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_resource emits inventory.resource.created with correct envelope fields.

    The payload preserves 'kind' field so that Step 22 cross-slice consumers
    can route on payload['kind'] when rewriting the xfailed ACL pipeline assertions.
    """
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='emit-c-001',
            application_id=app_id,
            kind='database',
        )
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.resource.created')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.resources'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(resource.id)
    assert envelope.causation_id is None
    assert isinstance(envelope.correlation_id, str)
    assert len(envelope.correlation_id) > 0
    assert envelope.payload == {
        'resource_id': str(resource.id),
        'application_id': str(app_id),
        'kind': 'database',
    }


@pytest.mark.asyncio
async def test_update_resource_emits_inventory_resource_updated(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_resource emits inventory.resource.updated with changed_fields and correct target_id."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='emit-u-001',
            application_id=app_id,
            kind='api',
        )
        await session.commit()
        resource_id = resource.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = ResourcePatch(privilege_level='admin')
        await service.update_resource(session, resource_id, patch)
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.resource.updated')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.target_id == str(resource_id)
    assert envelope.payload['changed_fields'] == ['privilege_level']
    assert envelope.payload['resource_id'] == str(resource_id)


@pytest.mark.asyncio
async def test_add_attribute_emits_inventory_resource_attribute_added(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute emits inventory.resource.attribute_added with owning-parent target_id."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='emit-aa-001',
            application_id=app_id,
            kind='storage',
        )
        await session.flush()
        capturing_events.emitted.clear()
        await service.add_attribute(session, resource.id, 'owner', 'alice')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.resource.attribute_added')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.target_id == str(resource.id)
    assert envelope.payload == {'resource_id': str(resource.id), 'key': 'owner'}


@pytest.mark.asyncio
async def test_remove_attribute_emits_inventory_resource_attribute_removed(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """remove_attribute emits inventory.resource.attribute_removed with owning-parent target_id."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='emit-ar-001',
            application_id=app_id,
            kind='queue',
        )
        await session.flush()
        await service.add_attribute(session, resource.id, 'owner', 'bob')
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        capturing_events.emitted.clear()
        await service.remove_attribute(session, resource_id, 'owner')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.resource.attribute_removed')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.target_id == str(resource_id)
    assert envelope.payload == {'resource_id': str(resource_id), 'key': 'owner'}


# ---------------------------------------------------------------------------
# Drop-retrieved test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_resource_does_not_emit_event(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_resource emits no event (Q1 — resource.retrieved dropped, audit deferred)."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='no-emit-001',
            application_id=app_id,
            kind='folder',
        )
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        capturing_events.emitted.clear()
        result = await service.get_resource(session, resource_id)
        assert result is not None
        assert capturing_events.emitted == []

    async with session_factory() as session:
        result = await service.get_resource(session, uuid.uuid4())
        assert result is None
        assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Correlation-id tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_resource_correlation_id_explicit(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_resource passes explicit correlation_id through to the envelope."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.create_resource(
            session,
            external_id='corr-e-001',
            application_id=app_id,
            kind='table',
            correlation_id='trace-xyz-789',
        )
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.resource.created')
    assert len(envelopes) == 1
    assert envelopes[0].correlation_id == 'trace-xyz-789'


@pytest.mark.asyncio
async def test_create_resource_correlation_id_autogenerated(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_resource autogenerates a 32-hex correlation_id when none is supplied."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.create_resource(
            session,
            external_id='corr-a-001',
            application_id=app_id,
            kind='view',
        )
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.resource.created')
    assert len(envelopes) == 1
    corr_id = envelopes[0].correlation_id
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


@pytest.mark.asyncio
async def test_update_resource_correlation_id_autogenerated_independent_of_create(
    service: ResourceService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_resource autogenerates its own correlation_id independently of the create correlation_id."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='corr-ind-001',
            application_id=app_id,
            kind='bucket',
            correlation_id='A',
        )
        await session.commit()
        resource_id = resource.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = ResourcePatch(privilege_level='read')
        await service.update_resource(session, resource_id, patch)
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.resource.updated')
    assert len(envelopes) == 1
    corr_id = envelopes[0].correlation_id
    assert corr_id != 'A'
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


# ---------------------------------------------------------------------------
# Anti-dual-emit guard
# ---------------------------------------------------------------------------


def test_resource_service_has_no_log_attribute() -> None:
    """ResourceService must not carry a _log attribute (anti-dual-emit guard)."""
    service = ResourceService()
    assert getattr(service, '_log', None) is None


# ---------------------------------------------------------------------------
# Phase 12 Step 6 — identity triple tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_resource_with_explicit_identity(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource persists explicit resource_type / resource_key verbatim."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='identity-explicit-001',
            application_id=app_id,
            kind='table',
            resource_type='snowflake_table',
            resource_key='finance.public.orders',
        )
        await session.commit()

    assert resource.resource_type == 'snowflake_table'
    assert resource.resource_key == 'finance.public.orders'


@pytest.mark.asyncio
async def test_create_resource_defaults_identity_from_kind_and_external_id(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource defaults resource_type = kind, resource_key = external_id when not provided."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='identity-default-001',
            application_id=app_id,
            kind='bucket',
        )
        await session.commit()

    assert resource.resource_type == 'bucket'
    assert resource.resource_key == 'identity-default-001'


@pytest.mark.asyncio
async def test_create_resource_identity_duplicate_raises(
    service: ResourceService,
    session_factory,
) -> None:
    """Two creates with same identity triple but different external_id raise DuplicateResourceError."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.create_resource(
            session,
            external_id='id-dup-first',
            application_id=app_id,
            kind='table',
            resource_type='pg_table',
            resource_key='public.events',
        )
        await session.commit()

    with pytest.raises(DuplicateResourceError):
        async with session_factory() as session:
            await service.create_resource(
                session,
                external_id='id-dup-second',
                application_id=app_id,
                kind='table',
                resource_type='pg_table',
                resource_key='public.events',
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_resource_by_identity_via_service(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource_by_identity returns the correct resource."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        created = await service.create_resource(
            session,
            external_id='identity-lookup-001',
            application_id=app_id,
            kind='view',
            resource_type='bq_view',
            resource_key='project.dataset.my_view',
        )
        await session.commit()
        resource_id = created.id

    async with session_factory() as session:
        found = await service.get_resource_by_identity(
            session,
            application_id=app_id,
            resource_type='bq_view',
            resource_key='project.dataset.my_view',
        )

    assert found is not None
    assert found.id == resource_id


@pytest.mark.asyncio
async def test_get_resource_by_identity_returns_none_when_absent(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource_by_identity returns None when the triple does not exist."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await session.commit()

    async with session_factory() as session:
        found = await service.get_resource_by_identity(
            session,
            application_id=app_id,
            resource_type='missing_type',
            resource_key='missing_key',
        )

    assert found is None


# ---------------------------------------------------------------------------
# ensure_resource_by_identity tests (Step 14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_resource_by_identity_creates_new(
    service: ResourceService,
    session_factory,
    capturing_events: CapturingEventService,
) -> None:
    """ensure_resource_by_identity creates a new Resource when triple doesn't exist."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.ensure_resource_by_identity(
            session,
            application_id=app_id,
            resource_type='table',
            resource_key='orders',
        )
        await session.commit()

    assert resource.id is not None
    assert resource.resource_type == 'table'
    assert resource.resource_key == 'orders'
    assert resource.external_id == 'orders'
    assert resource.kind == 'table'

    created = capturing_events.filter_by_type('inventory.resource.created')
    assert len(created) == 1


@pytest.mark.asyncio
async def test_ensure_resource_by_identity_returns_existing(
    service: ResourceService,
    session_factory,
    capturing_events: CapturingEventService,
) -> None:
    """ensure_resource_by_identity returns existing Resource without creating duplicate."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)

        r1 = await service.ensure_resource_by_identity(
            session,
            application_id=app_id,
            resource_type='table',
            resource_key='users',
        )
        r2 = await service.ensure_resource_by_identity(
            session,
            application_id=app_id,
            resource_type='table',
            resource_key='users',
        )
        await session.commit()

    assert r1.id == r2.id
    # Only one created event — second call is a read
    created = capturing_events.filter_by_type('inventory.resource.created')
    assert len(created) == 1


@pytest.mark.asyncio
async def test_ensure_resource_by_identity_different_keys_separate_rows(
    service: ResourceService,
    session_factory,
) -> None:
    """Different resource_keys produce different Resources."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        r1 = await service.ensure_resource_by_identity(
            session,
            application_id=app_id,
            resource_type='table',
            resource_key='table_a',
        )
        r2 = await service.ensure_resource_by_identity(
            session,
            application_id=app_id,
            resource_type='table',
            resource_key='table_b',
        )
        await session.commit()

    assert r1.id != r2.id
