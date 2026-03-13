# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from typing import NoReturn
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.applications.exceptions import (
    ApplicationCodeAlreadyExistsError,
    ApplicationNotFoundError,
)
from src.platform.applications.models import Application
from src.platform.applications.repository import get_application_by_id
from src.platform.applications.schemas import ApplicationCreate, ApplicationUpdate
from src.platform.logs.schemas import LogLevel, LogParticipantKind
from src.platform.logs.service import LogService, merge_emit_capability_trace_fields, noop_log_service


def _discriminate_integrity_error(exc: IntegrityError, code: str) -> NoReturn:
    """Raise ApplicationCodeAlreadyExistsError if the error is a code uniqueness violation.

    asyncpg wraps the original error as exc.orig.__cause__; constraint_name lives there.
    """
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    # constraint_name is on the underlying asyncpg exception, not on the SQLAlchemy wrapper
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint: str | None = getattr(asyncpg_exc, 'constraint_name', None)
    if pgcode == '23505' and constraint == 'uq_applications_code':
        raise ApplicationCodeAlreadyExistsError(f"Application with code '{code}' already exists") from None
    raise exc


async def create_application(
    session: AsyncSession,
    request: ApplicationCreate,
    log_service: LogService | None = None,
) -> Application:
    log = log_service if log_service is not None else noop_log_service
    app = Application(
        name=request.name,
        code=request.code,
        config=request.config,
        required_connector_tags=request.required_connector_tags,
        is_active=request.is_active,
    )
    session.add(app)
    try:
        await session.flush()
    except IntegrityError as exc:
        _discriminate_integrity_error(exc, request.code)
    await session.refresh(app)
    log.emit_safe(
        'application.created',
        LogLevel.INFO,
        'Application created',
        'applications',
        merge_emit_capability_trace_fields(
            {
                'application_id': str(app.id),
                'name': app.name,
                'code': app.code,
                'required_connector_tags': app.required_connector_tags,
            },
            capability_id='applications',
            target_id=str(app.id),
            target_type=LogParticipantKind.APPLICATION.value,
        ),
    )
    return app


async def update_application(
    session: AsyncSession,
    application_id: uuid.UUID,
    request: ApplicationUpdate,
    log_service: LogService | None = None,
) -> Application:
    log = log_service if log_service is not None else noop_log_service
    app = await get_application_by_id(session, application_id)
    if app is None:
        log.emit_safe(
            'application.not_found',
            LogLevel.WARNING,
            f'Application {application_id} not found',
            'applications',
            merge_emit_capability_trace_fields(
                {'application_id': str(application_id)},
                capability_id='applications',
                target_id=str(application_id),
                target_type=LogParticipantKind.APPLICATION.value,
            ),
        )
        raise ApplicationNotFoundError(f'Application {application_id} not found')
    if request.name is not None:
        app.name = request.name
    if request.code is not None:
        app.code = request.code
    if request.config is not None:
        app.config = request.config
    if request.required_connector_tags is not None:
        app.required_connector_tags = request.required_connector_tags
    if request.is_active is not None:
        app.is_active = request.is_active
    try:
        await session.flush()
    except IntegrityError as exc:
        _discriminate_integrity_error(exc, request.code or app.code)
    await session.refresh(app)
    log.emit_safe(
        'application.updated',
        LogLevel.INFO,
        'Application updated',
        'applications',
        merge_emit_capability_trace_fields(
            {
                'application_id': str(app.id),
                'code': app.code,
            },
            capability_id='applications',
            target_id=str(app.id),
            target_type=LogParticipantKind.APPLICATION.value,
        ),
    )
    return app


async def delete_application(
    session: AsyncSession,
    application_id: uuid.UUID,
    log_service: LogService | None = None,
) -> None:
    log = log_service if log_service is not None else noop_log_service
    app = await get_application_by_id(session, application_id)
    if app is None:
        log.emit_safe(
            'application.not_found',
            LogLevel.WARNING,
            f'Application {application_id} not found',
            'applications',
            merge_emit_capability_trace_fields(
                {'application_id': str(application_id)},
                capability_id='applications',
                target_id=str(application_id),
                target_type=LogParticipantKind.APPLICATION.value,
            ),
        )
        raise ApplicationNotFoundError(f'Application {application_id} not found')
    await session.delete(app)
    log.emit_safe(
        'application.deleted',
        LogLevel.INFO,
        'Application deleted',
        'applications',
        merge_emit_capability_trace_fields(
            {'application_id': str(application_id)},
            capability_id='applications',
            target_id=str(application_id),
            target_type=LogParticipantKind.APPLICATION.value,
        ),
    )
