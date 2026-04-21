# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for application service: code persistence, duplicate detection, event payloads."""

import pytest
from src.platform.applications.exceptions import ApplicationCodeAlreadyExistsError
from src.platform.applications.schemas import ApplicationCreate, ApplicationUpdate
from src.platform.applications.service import create_application, update_application
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent
from src.platform.logs.service import LogService


class _CaptureSink(LogSink):
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


def _make_log() -> tuple[LogService, _CaptureSink]:
    sink = _CaptureSink()
    factory = LogSinkFactory()
    factory.register('cap', lambda: sink)
    return LogService(factory, provider_name='cap'), sink


@pytest.mark.asyncio
async def test_create_application_persists_code(session_factory) -> None:
    log, _ = _make_log()
    async with session_factory() as session:
        app = await create_application(
            session,
            ApplicationCreate(name='AD Prod', code='ad'),
            log_service=log,
        )
        await session.commit()
    assert app.code == 'ad'


@pytest.mark.asyncio
async def test_create_application_emits_application_created_with_code(session_factory) -> None:
    log, sink = _make_log()
    async with session_factory() as session:
        await create_application(
            session,
            ApplicationCreate(name='AD Prod', code='ad'),
            log_service=log,
        )
        await session.commit()

    assert len(sink.events) == 1
    ev = sink.events[0]
    # Step 23: event_type no longer forwarded via emit_safe; check operational fields.
    assert ev.message == 'Application created'
    assert ev.payload.get('code') == 'ad'


@pytest.mark.asyncio
async def test_create_application_duplicate_code_raises_error(session_factory) -> None:
    log, _ = _make_log()
    async with session_factory() as session:
        await create_application(
            session,
            ApplicationCreate(name='AD Prod', code='ad'),
            log_service=log,
        )
        await session.commit()

    with pytest.raises(ApplicationCodeAlreadyExistsError):
        async with session_factory() as session:
            await create_application(
                session,
                ApplicationCreate(name='AD Stage', code='ad'),
                log_service=log,
            )
            await session.commit()


@pytest.mark.asyncio
async def test_update_application_changes_code(session_factory) -> None:
    log, sink = _make_log()
    async with session_factory() as session:
        app = await create_application(
            session,
            ApplicationCreate(name='AD Prod', code='ad'),
            log_service=log,
        )
        await session.commit()
        app_id = app.id

    sink.events.clear()
    async with session_factory() as session:
        updated = await update_application(
            session,
            app_id,
            ApplicationUpdate(code='ad-prod'),
            log_service=log,
        )
        await session.commit()

    assert updated.code == 'ad-prod'
    assert len(sink.events) == 1
    ev = sink.events[0]
    # Step 23: event_type no longer forwarded via emit_safe; check operational fields.
    assert ev.message == 'Application updated'
    assert ev.payload.get('code') == 'ad-prod'


@pytest.mark.asyncio
async def test_update_application_duplicate_code_raises_error(session_factory) -> None:
    log, _ = _make_log()
    async with session_factory() as session:
        await create_application(
            session,
            ApplicationCreate(name='AD Prod', code='ad'),
            log_service=log,
        )
        app2 = await create_application(
            session,
            ApplicationCreate(name='Jira', code='jira'),
            log_service=log,
        )
        await session.commit()
        app2_id = app2.id

    with pytest.raises(ApplicationCodeAlreadyExistsError):
        async with session_factory() as session:
            await update_application(
                session,
                app2_id,
                ApplicationUpdate(code='ad'),
                log_service=log,
            )
            await session.commit()


@pytest.mark.asyncio
async def test_update_application_none_code_keeps_existing(session_factory) -> None:
    log, _ = _make_log()
    async with session_factory() as session:
        app = await create_application(
            session,
            ApplicationCreate(name='AD Prod', code='ad'),
            log_service=log,
        )
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        updated = await update_application(
            session,
            app_id,
            ApplicationUpdate(name='AD Production'),
            log_service=log,
        )
        await session.commit()

    assert updated.code == 'ad'
    assert updated.name == 'AD Production'
