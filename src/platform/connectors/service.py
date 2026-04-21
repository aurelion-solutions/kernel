# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.applications.models import Application
from src.platform.connectors.exceptions import ConnectorInstanceNotFoundError
from src.platform.connectors.models import ConnectorInstance
from src.platform.connectors.repository import (
    delete_stale_connector_instances,
    get_connector_instance_by_instance_id,
    list_connector_instances,
    list_online_connector_instances,
    upsert_connector_instance,
)
from src.platform.connectors.selector import select_connector_instance_by_tags
from src.platform.logs.schemas import LogLevel, LogParticipantKind
from src.platform.logs.service import LogService, merge_emit_capability_trace_fields, noop_log_service


class ConnectorInstanceService:
    async def upsert_instance(
        self,
        session: AsyncSession,
        *,
        instance_id: str,
        tags: list[str],
    ) -> ConnectorInstance:
        return await upsert_connector_instance(
            session,
            instance_id=instance_id,
            tags=tags,
        )

    async def cleanup_stale_instances(
        self,
        session: AsyncSession,
        *,
        offline_for: timedelta = timedelta(days=1),
        log_service: LogService | None = None,
    ) -> int:
        log = log_service if log_service is not None else noop_log_service
        deleted = await delete_stale_connector_instances(
            session,
            offline_for=offline_for,
        )

        if deleted > 0:
            # NOTE: kwarg-shape refactor (Step 23 Phase 10) — NOT a migration to aurelion.events bus.
            log.emit_safe(
                level=LogLevel.INFO,
                message='Deleted stale connector instances',
                component='connectors',
                payload=merge_emit_capability_trace_fields(
                    {
                        'deleted_count': deleted,
                        'offline_for_seconds': int(offline_for.total_seconds()),
                    },
                    capability_id='connectors',
                    target_id='connector_registry',
                ),
            )

        return deleted

    async def register_from_message(
        self,
        session: AsyncSession,
        *,
        instance_id: str,
        tags: list[str],
        log_service: LogService | None = None,
    ) -> ConnectorInstance:
        log = log_service if log_service is not None else noop_log_service

        await self.cleanup_stale_instances(session, log_service=log)

        existing = await get_connector_instance_by_instance_id(session, instance_id)

        instance = await upsert_connector_instance(
            session,
            instance_id=instance_id,
            tags=tags,
        )

        if existing is None:
            # NOTE: kwarg-shape refactor (Step 23 Phase 10) — NOT a migration to aurelion.events bus.
            log.emit_safe(
                level=LogLevel.INFO,
                message='Connector instance registered',
                component='connectors',
                payload=merge_emit_capability_trace_fields(
                    {
                        'instance_id': instance.instance_id,
                        'tags': instance.tags,
                    },
                    capability_id='connectors',
                    target_id=instance.instance_id,
                    target_type=LogParticipantKind.CONNECTOR.value,
                ),
            )
        else:
            # NOTE: kwarg-shape refactor (Step 23 Phase 10) — NOT a migration to aurelion.events bus.
            log.emit_safe(
                level=LogLevel.INFO,
                message='Connector instance updated from registration',
                component='connectors',
                payload=merge_emit_capability_trace_fields(
                    {
                        'instance_id': instance.instance_id,
                        'tags': instance.tags,
                    },
                    capability_id='connectors',
                    target_id=instance.instance_id,
                    target_type=LogParticipantKind.CONNECTOR.value,
                ),
            )

        return instance

    async def get_instance(
        self,
        session: AsyncSession,
        instance_id: str,
    ) -> ConnectorInstance | None:
        return await get_connector_instance_by_instance_id(session, instance_id)

    async def list_instances(
        self,
        session: AsyncSession,
    ) -> list[ConnectorInstance]:
        return await list_connector_instances(session)

    async def select_instance_for_tags(
        self,
        session: AsyncSession,
        required_tags: list[str],
    ) -> ConnectorInstance | None:
        await self.cleanup_stale_instances(session)
        instances = await list_online_connector_instances(session)
        return select_connector_instance_by_tags(instances, required_tags)

    async def require_instance_id_for_application(
        self,
        session: AsyncSession,
        application: Application,
    ) -> str:
        """Pick an online connector instance matching ``application`` tags; raise if none."""
        instance = await self.select_instance_for_tags(
            session,
            application.required_connector_tags or [],
        )
        if instance is None:
            raise ConnectorInstanceNotFoundError(f'No connector instance found for application {application.id}')
        return instance.instance_id
