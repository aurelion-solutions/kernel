# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch service for coordinating data lake storage and PostgreSQL metadata."""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.lake_batches.models import LakeBatch
from src.inventory.lake_batches.repository import create_lake_batch, delete_by_id, get_by_id
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service
from src.platform.storage.factory import DataLakeStorageFactory, UnsupportedProviderError
from src.platform.storage.interface import DataLakeStorage

_COMPONENT = 'inventory.lake_batches'


class BatchNotFoundError(Exception):
    """Raised when a lake batch is not found."""

    def __init__(self, batch_id: uuid.UUID) -> None:
        self.batch_id = batch_id
        super().__init__(f'Lake batch not found: {batch_id}')


class LakeBatchService:
    """Orchestrates data lake storage and PostgreSQL batch metadata."""

    def __init__(
        self,
        storage_factory: DataLakeStorageFactory,
        log_service: LogService | None = None,
        event_service: EventService | None = None,
    ) -> None:
        self._factory = storage_factory
        self._log = log_service if log_service is not None else noop_log_service
        self._events = event_service if event_service is not None else noop_event_service

    def _get_storage(
        self,
        storage_provider: str,
        *,
        batch_id: str | None = None,
    ) -> DataLakeStorage:
        """Resolve storage. On failure, log and re-raise."""
        try:
            return self._factory.get(storage_provider)
        except UnsupportedProviderError:
            payload: dict[str, Any] = {'storage_provider': storage_provider}
            if batch_id:
                payload['batch_id'] = batch_id
            self._log.emit_safe(
                level=LogLevel.ERROR,
                message=f'Unsupported storage provider: {storage_provider!r}',
                component='data-lake',
                payload=merge_emit_log_participant_fields(
                    payload,
                    actor_component='data-lake',
                    target_id='storage',
                ),
            )
            raise

    async def create_batch(
        self,
        session: AsyncSession,
        storage_provider: str,
        dataset_type: str,
        records: Iterable[dict[str, Any]],
        task_id: uuid.UUID | None = None,
        application_id: uuid.UUID | None = None,
        content_type: str | None = None,
        metadata_json: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> LakeBatch:
        """Write records to lake, create metadata row, return LakeBatch."""
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='Lake batch write started',
            component='data-lake',
            payload=merge_emit_log_participant_fields(
                {'storage_provider': storage_provider, 'dataset_type': dataset_type},
                actor_component='data-lake',
                target_id='batch',
            ),
        )
        records_list = list(records)
        row_count = len(records_list)

        storage = self._get_storage(storage_provider)
        storage_key = storage.write_batch(dataset_type, records_list)

        batch = await create_lake_batch(
            session,
            storage_provider=storage_provider,
            dataset_type=dataset_type,
            storage_key=storage_key,
            row_count=row_count,
            application_id=application_id,
            task_id=task_id,
            content_type=content_type,
            metadata_json=metadata_json,
        )
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.lake_batch.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'batch_id': str(batch.id),
                    'storage_provider': storage_provider,
                    'dataset_type': dataset_type,
                    'storage_key': storage_key,
                    'row_count': row_count,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(batch.id),
            )
        )
        return batch

    async def get_batch(
        self,
        session: AsyncSession,
        batch_id: uuid.UUID,
    ) -> LakeBatch | None:
        """Load batch metadata by id."""
        return await get_by_id(session, batch_id)

    async def read_batch(
        self,
        session: AsyncSession,
        batch_id: uuid.UUID,
    ) -> Iterable[dict[str, Any]]:
        """Read batch payload from lake. Raises BatchNotFoundError if missing."""
        batch = await get_by_id(session, batch_id)
        if batch is None:
            raise BatchNotFoundError(batch_id)

        self._log.emit_safe(
            level=LogLevel.INFO,
            message='Lake batch read requested',
            component='data-lake',
            payload=merge_emit_log_participant_fields(
                {'batch_id': str(batch_id), 'storage_provider': batch.storage_provider},
                actor_component='data-lake',
                target_id='batch',
            ),
        )
        storage = self._get_storage(
            batch.storage_provider,
            batch_id=str(batch_id),
        )
        return storage.read_batch(batch.storage_key)

    async def delete_batch(
        self,
        session: AsyncSession,
        batch_id: uuid.UUID,
        delete_payload: bool = True,
        correlation_id: str | None = None,
    ) -> None:
        """Delete batch metadata and optionally lake payload."""
        batch = await get_by_id(session, batch_id)
        if batch is None:
            raise BatchNotFoundError(batch_id)

        if delete_payload:
            storage = self._get_storage(
                batch.storage_provider,
                batch_id=str(batch_id),
            )
            storage.delete_batch(batch.storage_key)

        await delete_by_id(session, batch_id)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.lake_batch.deleted',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'batch_id': str(batch_id),
                    'storage_provider': batch.storage_provider,
                    'storage_key': batch.storage_key,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(batch.id),
            )
        )
