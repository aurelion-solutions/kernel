# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SecretService — KEEP-variant Phase 10 Step 19 rewrite."""

import asyncio
import json
from pathlib import Path

import pytest
from src.inventory.secrets.service import SecretService
from src.platform.events.schemas import EventParticipantKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService
from src.platform.secrets.factory import SecretManagerFactory, UnsupportedProviderError
from src.platform.secrets.providers.file import FileSecretManager


@pytest.fixture
def file_factory(tmp_path: Path) -> SecretManagerFactory:
    factory = SecretManagerFactory()
    factory.register('file', lambda: FileSecretManager(path=tmp_path / 'secrets.json'))
    return factory


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(
    file_factory: SecretManagerFactory,
    event_service: EventService,
) -> SecretService:
    # log_service omitted → falls back to noop_log_service
    return SecretService(factory=file_factory, event_service=event_service)


# ---------------------------------------------------------------------------
# Behavioural tests (kept from prior file)
# ---------------------------------------------------------------------------


async def test_create_secret_then_get_secret_returns_value(service: SecretService) -> None:
    """create_secret then get_secret returns the stored value."""
    await service.create_secret(key='app/token', provider='file', namespace='default', value='secret123')
    result = service.get_secret(key='app/token', provider='file', namespace='default')
    assert result == 'secret123'


async def test_delete_secret_then_get_secret_raises(service: SecretService) -> None:
    """delete_secret then get_secret raises KeyError."""
    await service.create_secret(key='to_delete', provider='file', namespace='ns', value='x')
    await service.delete_secret(key='to_delete', provider='file', namespace='ns')
    with pytest.raises(KeyError, match=r"Secret not found: 'ns/to_delete'"):
        service.get_secret(key='to_delete', provider='file', namespace='ns')


def test_unknown_provider_raises_unsupported_provider_error(service: SecretService) -> None:
    """Unknown provider raises UnsupportedProviderError."""
    with pytest.raises(UnsupportedProviderError, match=r"Unsupported secret provider: 'unknown'"):
        service.get_secret(key='a/b', provider='unknown', namespace='default')


def test_missing_key_raises_key_error(service: SecretService) -> None:
    """get_secret with non-existent key raises KeyError."""
    with pytest.raises(KeyError, match=r"Secret not found: 'default/missing'"):
        service.get_secret(key='missing', provider='file', namespace='default')


# ---------------------------------------------------------------------------
# Event-bus tests
# ---------------------------------------------------------------------------


async def test_create_secret_emits_inventory_secret_created_event(
    service: SecretService,
    capturing_events: CapturingEventService,
) -> None:
    """create_secret emits inventory.secret.created via EventService."""
    await service.create_secret(key='app_token', provider='file', namespace='default', value='val')

    emitted = capturing_events.filter_by_type('inventory.secret.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.secrets'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    # target_id is the provider storage key (namespace/key), NOT a DB UUID —
    # divergence from lake_batches (target_id=str(batch.id)): service is synchronous
    # and emits before create_secret_metadata in routes, so no Secret.id is available.
    assert envelope.target_id == 'default/app_token'
    assert envelope.payload == {
        'provider': 'file',
        'key': 'app_token',
        'namespace': 'default',
        'storage_key': 'default/app_token',
    }
    assert 'value' not in envelope.payload


async def test_delete_secret_emits_inventory_secret_deleted_event(
    service: SecretService,
    capturing_events: CapturingEventService,
) -> None:
    """delete_secret emits inventory.secret.deleted via EventService."""
    await service.create_secret(key='app_token', provider='file', namespace='default', value='val')
    capturing_events.emitted.clear()

    await service.delete_secret(key='app_token', provider='file', namespace='default')

    emitted = capturing_events.filter_by_type('inventory.secret.deleted')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.secrets'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    # target_id is the storage key — same divergence as create (see above).
    assert envelope.target_id == 'default/app_token'
    assert envelope.payload == {
        'provider': 'file',
        'key': 'app_token',
        'namespace': 'default',
        'storage_key': 'default/app_token',
    }
    assert 'value' not in envelope.payload


# ---------------------------------------------------------------------------
# D4 override: secret.retrieved stays on log bus as INFO (not event bus)
# ---------------------------------------------------------------------------


async def test_secret_retrieved_emits_info_log_without_event_type(
    tmp_path: Path,
    file_factory: SecretManagerFactory,
) -> None:
    """D4 override: get_secret emits INFO log only; event bus receives nothing."""
    log_path = tmp_path / 'retrieved.jsonl'
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory, provider_name='file')

    capturing = CapturingEventService()
    event_svc = EventService(sink=capturing)

    svc = SecretService(
        factory=file_factory,
        log_service=log_service,
        event_service=event_svc,
    )

    await svc.create_secret(key='app_token', provider='file', namespace='default', value='v')
    capturing.emitted.clear()

    result = svc.get_secret(key='app_token', provider='file', namespace='default')
    assert result == 'v'

    # emit_safe schedules a task on the running loop — yield to let it run
    await asyncio.sleep(0)

    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]

    # (a) at least one INFO record with expected fields
    retrieved = [
        r
        for r in records
        if r.get('component') == 'inventory.secrets'
        and r.get('message') == 'Secret retrieved'
        and r.get('payload', {}).get('key') == 'app_token'
    ]
    assert len(retrieved) >= 1

    # (b) no record carries event_type (KEEP-variant anti-dual-emit guard)
    for record in records:
        assert 'event_type' not in record, f'Unexpected event_type in log record: {record}'

    # (c) event bus received nothing for the retrieval path
    assert capturing.emitted == []


# ---------------------------------------------------------------------------
# Provider-failure log test (rewritten per C6 / C11)
# ---------------------------------------------------------------------------


async def test_provider_resolution_failure_emits_error_log_without_event_type_and_re_raises(
    tmp_path: Path,
    file_factory: SecretManagerFactory,
) -> None:
    """Provider resolution failure emits ERROR log without event_type and re-raises."""
    log_path = tmp_path / 'provider_fail.jsonl'
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory, provider_name='file')

    capturing = CapturingEventService()
    event_svc = EventService(sink=capturing)

    svc = SecretService(
        factory=file_factory,
        log_service=log_service,
        event_service=event_svc,
    )

    with pytest.raises(UnsupportedProviderError, match=r"Unsupported secret provider: 'unknown'"):
        svc.get_secret(key='a/b', provider='unknown', namespace='default')

    # emit_safe schedules a task on the running loop — yield to let it run
    await asyncio.sleep(0)

    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    error_records = [r for r in records if r.get('level') == 'error']
    assert len(error_records) >= 1
    failed = error_records[0]
    assert 'Secret provider resolution failed' in failed['message']
    assert failed['payload']['provider'] == 'unknown'
    # KEEP-variant anti-dual-emit: operational log must NOT carry event_type
    assert 'event_type' not in failed
    # Provider-failure branch never reaches the event emit site
    assert capturing.emitted == []


# ---------------------------------------------------------------------------
# Anti-dual-emit guard tests (C11 fixture isolation requirement)
# ---------------------------------------------------------------------------


async def test_create_secret_does_not_dual_emit_on_log_and_event_bus(
    tmp_path: Path,
    file_factory: SecretManagerFactory,
) -> None:
    """KEEP-variant: create_secret emits event; log records have no event_type, no legacy message."""
    log_path = tmp_path / 'create_guard.jsonl'
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory, provider_name='file')

    capturing = CapturingEventService()
    event_svc = EventService(sink=capturing)

    # Construct service with BOTH buses wired (per C11 isolation requirement)
    svc = SecretService(
        factory=file_factory,
        log_service=log_service,
        event_service=event_svc,
    )

    await svc.create_secret(key='dual_key', provider='file', namespace='ns', value='x')

    # Event bus received the domain event
    assert len(capturing.filter_by_type('inventory.secret.created')) == 1

    # Log records must not carry any event_type key
    if log_path.exists():
        log_records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
        for record in log_records:
            assert 'event_type' not in record, f'Unexpected event_type in log: {record}'
        # Legacy 'Secret created' log must not exist
        assert [r for r in log_records if r.get('message') == 'Secret created'] == []


async def test_delete_secret_does_not_dual_emit_on_log_and_event_bus(
    tmp_path: Path,
    file_factory: SecretManagerFactory,
) -> None:
    """KEEP-variant: delete_secret emits event; log records have no event_type, no legacy message."""
    log_path = tmp_path / 'delete_guard.jsonl'
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory, provider_name='file')

    capturing = CapturingEventService()
    event_svc = EventService(sink=capturing)

    svc = SecretService(
        factory=file_factory,
        log_service=log_service,
        event_service=event_svc,
    )

    await svc.create_secret(key='del_key', provider='file', namespace='ns', value='y')
    capturing.emitted.clear()

    await svc.delete_secret(key='del_key', provider='file', namespace='ns')

    # Event bus received the domain event
    assert len(capturing.filter_by_type('inventory.secret.deleted')) == 1

    # Log records must not carry any event_type key
    if log_path.exists():
        log_records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
        for record in log_records:
            assert 'event_type' not in record, f'Unexpected event_type in log: {record}'
        # Legacy 'Secret deleted' log must not exist
        assert [r for r in log_records if r.get('message') == 'Secret deleted'] == []


# ---------------------------------------------------------------------------
# correlation_id propagation (parametrised, 4 cases)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'method,explicit_corr_id',
    [
        ('create_secret', 'trace-sec-xyz'),
        ('create_secret', None),
        ('delete_secret', 'trace-sec-xyz'),
        ('delete_secret', None),
    ],
)
async def test_correlation_id_propagates_to_created_and_deleted_events(
    method: str,
    explicit_corr_id: str | None,
    file_factory: SecretManagerFactory,
    capturing_events: CapturingEventService,
    event_service: EventService,
) -> None:
    """correlation_id kwarg is forwarded to emitted event envelope."""
    svc = SecretService(factory=file_factory, event_service=event_service)

    if method == 'create_secret':
        await svc.create_secret(
            key='corr_key',
            provider='file',
            namespace='default',
            value='v',
            correlation_id=explicit_corr_id,
        )
        event_type = 'inventory.secret.created'
    else:
        # create first, then clear, then delete
        await svc.create_secret(key='corr_key', provider='file', namespace='default', value='v')
        capturing_events.emitted.clear()
        await svc.delete_secret(
            key='corr_key',
            provider='file',
            namespace='default',
            correlation_id=explicit_corr_id,
        )
        event_type = 'inventory.secret.deleted'

    emitted = capturing_events.filter_by_type(event_type)
    assert len(emitted) == 1
    envelope = emitted[0]
    assert isinstance(envelope.correlation_id, str)

    if explicit_corr_id is not None:
        assert envelope.correlation_id == explicit_corr_id
    else:
        # auto-generated: uuid4().hex shape — 32 lowercase hex chars
        assert len(envelope.correlation_id) == 32
        assert envelope.correlation_id == envelope.correlation_id.lower()
        assert all(c in '0123456789abcdef' for c in envelope.correlation_id)
