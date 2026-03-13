# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SecretService for provider-agnostic secret operations."""

from src.inventory.secrets.schemas import SecretCreate, SecretDelete, SecretRead
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service
from src.platform.secrets.factory import SecretManagerFactory, UnsupportedProviderError
from src.platform.secrets.interface import SecretManager


def _build_storage_key(namespace: str, key: str) -> str:
    """Build provider storage key from namespace and key."""
    return f'{namespace}/{key}'


class SecretService:
    """Resolves providers via factory and delegates. No secret values in DB."""

    def __init__(
        self,
        factory: SecretManagerFactory,
        log_service: LogService | None = None,
    ) -> None:
        self._factory = factory
        self._log = log_service if log_service is not None else noop_log_service

    def _get_manager(self, provider: str) -> SecretManager:
        """Resolve provider. On failure, log and re-raise."""
        try:
            return self._factory.get(provider)
        except UnsupportedProviderError:
            self._log.emit_safe(
                'secret.provider.failed',
                LogLevel.ERROR,
                f'Provider resolution failed: {provider!r}',
                'secret-manager',
                merge_emit_log_participant_fields(
                    {'provider': provider},
                    actor_component='secret-manager',
                    target_id='secret',
                ),
            )
            raise

    def create_secret(self, key: str, provider: str, namespace: str, value: str) -> None:
        """Store a secret. Validates key/namespace via schemas."""
        SecretCreate(key=key, provider=provider, namespace=namespace, value=value)
        manager = self._get_manager(provider)
        storage_key = _build_storage_key(namespace, key)
        manager.set_secret(storage_key, value)
        self._log.emit_safe(
            'secret.created',
            LogLevel.INFO,
            'Secret created',
            'secret-manager',
            merge_emit_log_participant_fields(
                {'provider': provider, 'key': key, 'namespace': namespace},
                actor_component='secret-manager',
                target_id='secret',
            ),
        )

    def get_secret(self, key: str, provider: str, namespace: str) -> str:
        """Retrieve a secret. Validates key/namespace via schemas."""
        SecretRead(key=key, provider=provider, namespace=namespace)
        manager = self._get_manager(provider)
        storage_key = _build_storage_key(namespace, key)
        value = manager.get_secret(storage_key)
        self._log.emit_safe(
            'secret.retrieved',
            LogLevel.INFO,
            'Secret retrieved',
            'secret-manager',
            merge_emit_log_participant_fields(
                {'provider': provider, 'key': key, 'namespace': namespace},
                actor_component='secret-manager',
                target_id='secret',
            ),
        )
        return value

    def delete_secret(self, key: str, provider: str, namespace: str) -> None:
        """Remove a secret. Validates key/namespace via schemas."""
        SecretDelete(key=key, provider=provider, namespace=namespace)
        manager = self._get_manager(provider)
        storage_key = _build_storage_key(namespace, key)
        manager.delete_secret(storage_key)
        self._log.emit_safe(
            'secret.deleted',
            LogLevel.INFO,
            'Secret deleted',
            'secret-manager',
            merge_emit_log_participant_fields(
                {'provider': provider, 'key': key, 'namespace': namespace},
                actor_component='secret-manager',
                target_id='secret',
            ),
        )
