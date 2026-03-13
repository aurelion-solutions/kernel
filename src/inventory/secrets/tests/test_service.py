# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SecretService."""

import json
from pathlib import Path

import pytest
from src.inventory.secrets.service import SecretService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService
from src.platform.secrets.factory import SecretManagerFactory, UnsupportedProviderError
from src.platform.secrets.providers.file import FileSecretManager


@pytest.fixture
def file_factory(tmp_path: Path) -> SecretManagerFactory:
    """Factory with file provider using tmp_path."""
    factory = SecretManagerFactory()
    factory.register('file', lambda: FileSecretManager(path=tmp_path / 'secrets.json'))
    return factory


@pytest.fixture
def service(file_factory: SecretManagerFactory) -> SecretService:
    return SecretService(factory=file_factory)


def test_create_secret_then_get_secret_returns_value(service: SecretService) -> None:
    """create_secret then get_secret returns the stored value."""
    service.create_secret(key='app/token', provider='file', namespace='default', value='secret123')
    result = service.get_secret(key='app/token', provider='file', namespace='default')
    assert result == 'secret123'


def test_delete_secret_then_get_secret_raises(service: SecretService) -> None:
    """delete_secret then get_secret raises KeyError."""
    service.create_secret(key='to_delete', provider='file', namespace='ns', value='x')
    service.delete_secret(key='to_delete', provider='file', namespace='ns')
    with pytest.raises(KeyError, match=r"Secret not found: 'ns/to_delete'"):
        service.get_secret(key='to_delete', provider='file', namespace='ns')


def test_unknown_provider_raises_unsupported_provider_error(service: SecretService) -> None:
    """Unknown provider raises UnsupportedProviderError."""
    with pytest.raises(UnsupportedProviderError, match=r"Unsupported secret provider: 'unknown'"):
        service.get_secret(key='a/b', provider='unknown', namespace='default')


def test_provider_resolution_failure_logs_and_re_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider resolution failure emits secret.provider.failed and re-raises."""
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'file')
    secret_factory = SecretManagerFactory()
    secret_factory.register('file', lambda: FileSecretManager(path=tmp_path / 'secrets.json'))
    log_path = tmp_path / 'provider_fail.jsonl'
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory)
    service = SecretService(factory=secret_factory, log_service=log_service)

    with pytest.raises(UnsupportedProviderError, match=r"Unsupported secret provider: 'unknown'"):
        service.get_secret(key='a/b', provider='unknown', namespace='default')

    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    failed = [r for r in records if r.get('event_type') == 'secret.provider.failed']
    assert len(failed) == 1
    assert failed[0]['payload']['provider'] == 'unknown'


def test_missing_key_raises_key_error(service: SecretService) -> None:
    """get_secret with non-existent key raises KeyError."""
    with pytest.raises(KeyError, match=r"Secret not found: 'default/missing'"):
        service.get_secret(key='missing', provider='file', namespace='default')


def test_secret_create_flow_emits_secret_created_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Secret create flow emits a log event with secret.created."""
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'file')
    secret_factory = SecretManagerFactory()
    secret_factory.register('file', lambda: FileSecretManager(path=tmp_path / 'secrets.json'))
    log_path = tmp_path / 'logs.jsonl'
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory)
    service = SecretService(factory=secret_factory, log_service=log_service)

    service.create_secret(key='smoke/key', provider='file', namespace='default', value='x')

    assert log_path.exists()
    lines = log_path.read_text().strip().split('\n')
    assert len(lines) >= 1
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'secret.created']
    assert len(created) == 1
    assert created[0]['component'] == 'secret-manager'
