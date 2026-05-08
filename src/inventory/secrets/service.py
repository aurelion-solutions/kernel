# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SecretService for provider-agnostic secret operations."""

from datetime import UTC, datetime
import uuid

from src.inventory.secrets.schemas import SecretCreate, SecretDelete, SecretRead
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service
from src.platform.secrets.factory import SecretManagerFactory, UnsupportedProviderError
from src.platform.secrets.interface import SecretManager

_COMPONENT = 'inventory.secrets'


def _build_storage_key(namespace: str, key: str) -> str:
    """Build provider storage key from namespace and key."""
    return f'{namespace}/{key}'


class SecretService:
    """Resolves providers via factory, delegates, emits domain events. No secret values in DB."""

    def __init__(
        self,
        factory: SecretManagerFactory,
        log_service: LogService | None = None,
        event_service: EventService | None = None,
    ) -> None:
        self._factory = factory
        self._log = log_service if log_service is not None else noop_log_service
        self._events = event_service if event_service is not None else noop_event_service

    def _get_manager(self, provider: str) -> SecretManager:
        """Resolve provider. On failure, emit ERROR log and re-raise."""
        try:
            return self._factory.get(provider)
        except UnsupportedProviderError:
            self._log.emit_safe(
                level=LogLevel.ERROR,
                message=f'Secret provider resolution failed: {provider!r}',
                component=_COMPONENT,
                payload=merge_emit_log_participant_fields(
                    {'provider': provider},
                    actor_component=_COMPONENT,
                    target_id='secret',
                ),
            )
            raise

    async def create_secret(
        self,
        key: str,
        provider: str,
        namespace: str,
        value: str,
        correlation_id: str | None = None,
    ) -> None:
        """Store a secret. Validates key/namespace via schemas. Emits inventory.secret.created."""
        SecretCreate(key=key, provider=provider, namespace=namespace, value=value)
        manager = self._get_manager(provider)
        storage_key = _build_storage_key(namespace, key)
        manager.set_secret(storage_key, value)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.secret.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'provider': provider,
                    'key': key,
                    'namespace': namespace,
                    'storage_key': storage_key,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=storage_key,
            )
        )

    def get_secret(self, key: str, provider: str, namespace: str) -> str:
        """Retrieve a secret. Validates inputs. Emits INFO log (not event — D4 override)."""
        SecretRead(key=key, provider=provider, namespace=namespace)
        manager = self._get_manager(provider)
        storage_key = _build_storage_key(namespace, key)
        value = manager.get_secret(storage_key)
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='Secret retrieved',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'provider': provider, 'key': key, 'namespace': namespace},
                actor_component=_COMPONENT,
                target_id='secret',
            ),
        )
        return value

    async def delete_secret(
        self,
        key: str,
        provider: str,
        namespace: str,
        correlation_id: str | None = None,
    ) -> None:
        """Remove a secret. Validates inputs. Emits inventory.secret.deleted."""
        SecretDelete(key=key, provider=provider, namespace=namespace)
        manager = self._get_manager(provider)
        storage_key = _build_storage_key(namespace, key)
        manager.delete_secret(storage_key)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.secret.deleted',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'provider': provider,
                    'key': key,
                    'namespace': namespace,
                    'storage_key': storage_key,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=storage_key,
            )
        )
