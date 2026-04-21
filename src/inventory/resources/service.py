# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resource service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.resources.models import (
    Resource,
    ResourceAttribute,
    ResourceDataSensitivity,
    ResourceEnvironment,
    ResourcePrivilegeLevel,
)
from src.inventory.resources.repository import (
    create_resource as repo_create_resource,
)
from src.inventory.resources.repository import (
    create_resource_attribute as repo_create_resource_attribute,
)
from src.inventory.resources.repository import (
    delete_resource_attribute as repo_delete_resource_attribute,
)
from src.inventory.resources.repository import (
    get_resource_by_application_and_external_id as repo_get_resource_by_application_and_external_id,
)
from src.inventory.resources.repository import (
    get_resource_by_id as repo_get_resource_by_id,
)
from src.inventory.resources.repository import (
    list_resource_attributes as repo_list_resource_attributes,
)
from src.inventory.resources.repository import (
    list_resources as repo_list_resources,
)
from src.inventory.resources.repository import (
    update_resource as repo_update_resource,
)
from src.inventory.resources.schemas import ResourcePatch
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.resources'


class ResourceNotFoundError(Exception):
    """Raised when a resource is not found."""

    def __init__(self, resource_id: uuid.UUID) -> None:
        self.resource_id = resource_id
        super().__init__(f'Resource not found: {resource_id}')


class ResourceApplicationNotFoundError(Exception):
    """Raised when the referenced application does not exist."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application not found: {application_id}')


class ResourceParentNotFoundError(Exception):
    """Raised when the referenced parent resource does not exist."""

    def __init__(self, parent_id: uuid.UUID) -> None:
        self.parent_id = parent_id
        super().__init__(f'Parent resource not found: {parent_id}')


class DuplicateResourceError(Exception):
    """Raised when a resource with the same (application_id, external_id) already exists."""

    def __init__(self, application_id: uuid.UUID, external_id: str) -> None:
        self.application_id = application_id
        self.external_id = external_id
        super().__init__(f'Duplicate resource: application_id={application_id}, external_id={external_id}')


class DuplicateResourceAttributeError(Exception):
    """Raised when adding an attribute with a key that already exists for the resource."""

    def __init__(self, resource_id: uuid.UUID, key: str) -> None:
        self.resource_id = resource_id
        self.key = key
        super().__init__(f'Duplicate attribute key for resource: {key}')


class ResourceAttributeNotFoundError(Exception):
    """Raised when a resource attribute is not found."""

    def __init__(self, resource_id: uuid.UUID, key: str) -> None:
        self.resource_id = resource_id
        self.key = key
        super().__init__(f'Resource attribute not found: {resource_id} / {key}')


async def _application_exists(session: AsyncSession, application_id: uuid.UUID) -> bool:
    """Check application existence via ORM model lookup."""
    from src.platform.applications.models import Application

    result = await session.get(Application, application_id)
    return result is not None


class ResourceService:
    """Orchestrates resource CRUD and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_resource(
        self,
        session: AsyncSession,
        *,
        external_id: str,
        application_id: uuid.UUID,
        kind: str,
        parent_id: uuid.UUID | None = None,
        path: str | None = None,
        description: str | None = None,
        privilege_level: ResourcePrivilegeLevel | None = None,
        environment: ResourceEnvironment | None = None,
        data_sensitivity: ResourceDataSensitivity | None = None,
        correlation_id: str | None = None,
    ) -> Resource:
        """Create a resource. Pre-validates application_id and parent_id. Emits inventory.resource.created."""
        if not await _application_exists(session, application_id):
            raise ResourceApplicationNotFoundError(application_id)

        if parent_id is not None:
            parent = await repo_get_resource_by_id(session, parent_id)
            if parent is None:
                raise ResourceParentNotFoundError(parent_id)

        try:
            resource = await repo_create_resource(
                session,
                external_id=external_id,
                application_id=application_id,
                kind=kind,
                parent_id=parent_id,
                path=path,
                description=description,
                privilege_level=privilege_level,
                environment=environment,
                data_sensitivity=data_sensitivity,
            )
        except IntegrityError as exc:
            orig = exc.orig
            pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
            if pgcode == '23505':
                raise DuplicateResourceError(application_id, external_id) from None
            raise

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.resource.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'resource_id': str(resource.id),
                    'application_id': str(application_id),
                    'kind': kind,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(resource.id),
            )
        )
        return resource

    async def get_resource_by_external_id(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID,
        external_id: str,
    ) -> Resource | None:
        """Look up resource by (application_id, external_id). Silent — no event emitted."""
        return await repo_get_resource_by_application_and_external_id(session, application_id, external_id)

    async def get_resource(
        self,
        session: AsyncSession,
        resource_id: uuid.UUID,
    ) -> Resource | None:
        """Get resource by id. No event emitted (Q1 — resource.retrieved dropped, audit deferred)."""
        return await repo_get_resource_by_id(session, resource_id)

    async def list_resources(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID | None = None,
        kind: str | None = None,
        privilege_level: ResourcePrivilegeLevel | None = None,
        environment: ResourceEnvironment | None = None,
        data_sensitivity: ResourceDataSensitivity | None = None,
    ) -> list[Resource]:
        """List resources. No event emitted."""
        return await repo_list_resources(
            session,
            application_id=application_id,
            kind=kind,
            privilege_level=privilege_level,
            environment=environment,
            data_sensitivity=data_sensitivity,
        )

    async def update_resource(
        self,
        session: AsyncSession,
        resource_id: uuid.UUID,
        patch: ResourcePatch,
        correlation_id: str | None = None,
    ) -> Resource:
        """Apply partial update to resource. Uses model_fields_set. Emits inventory.resource.updated if fields changed."""  # noqa: E501
        resource = await repo_get_resource_by_id(session, resource_id)
        if resource is None:
            raise ResourceNotFoundError(resource_id)

        fields_to_update = {field: getattr(patch, field) for field in patch.model_fields_set}

        if 'parent_id' in fields_to_update and fields_to_update['parent_id'] is not None:
            parent = await repo_get_resource_by_id(session, fields_to_update['parent_id'])
            if parent is None:
                raise ResourceParentNotFoundError(fields_to_update['parent_id'])

        changed_fields = await repo_update_resource(
            session,
            resource,
            fields_to_update=fields_to_update,
        )

        if changed_fields:
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='inventory.resource.updated',
                    occurred_at=datetime.now(UTC),
                    correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                    causation_id=None,
                    payload={
                        'resource_id': str(resource_id),
                        'changed_fields': sorted(changed_fields),
                    },
                    actor_kind=EventParticipantKind.CAPABILITY,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=str(resource.id),
                )
            )
        return resource

    async def list_attributes(
        self,
        session: AsyncSession,
        resource_id: uuid.UUID,
    ) -> list[ResourceAttribute]:
        """List attributes for a resource. Raises ResourceNotFoundError if missing."""
        resource = await repo_get_resource_by_id(session, resource_id)
        if resource is None:
            raise ResourceNotFoundError(resource_id)
        return await repo_list_resource_attributes(session, resource_id)

    async def add_attribute(
        self,
        session: AsyncSession,
        resource_id: uuid.UUID,
        key: str,
        value: str,
        correlation_id: str | None = None,
    ) -> ResourceAttribute:
        """Add attribute to resource. Emits inventory.resource.attribute_added. Raises on duplicate."""
        resource = await repo_get_resource_by_id(session, resource_id)
        if resource is None:
            raise ResourceNotFoundError(resource_id)
        try:
            attr = await repo_create_resource_attribute(
                session,
                resource_id=resource_id,
                key=key,
                value=value,
            )
        except IntegrityError:
            raise DuplicateResourceAttributeError(resource_id, key) from None
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.resource.attribute_added',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'resource_id': str(resource_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(resource_id),
            )
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        resource_id: uuid.UUID,
        key: str,
        correlation_id: str | None = None,
    ) -> None:
        """Remove attribute from resource. Emits inventory.resource.attribute_removed. Raises if missing."""
        resource = await repo_get_resource_by_id(session, resource_id)
        if resource is None:
            raise ResourceNotFoundError(resource_id)
        deleted = await repo_delete_resource_attribute(session, resource_id, key)
        if not deleted:
            raise ResourceAttributeNotFoundError(resource_id, key)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.resource.attribute_removed',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'resource_id': str(resource_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(resource_id),
            )
        )
