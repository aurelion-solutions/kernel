# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake migration API routes.

Exposes:
  POST /lake-migrations                  — start (or resume) migration job
  GET  /lake-migrations/{id}             — get run by id
  GET  /lake-migrations                  — list runs (paginated)

POST is non-blocking: schedules migration via BackgroundTasks, returns 202.
``dataset='all'`` runs both datasets sequentially and returns a list of two runs.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db, get_session_factory
from src.core.http.errors import translate_service_errors
from src.engines.lake_migration.deps import get_lake_migration_service
from src.engines.lake_migration.exceptions import (
    LakeMigrationConflictError,
    LakeMigrationDatasetError,
    LakeMigrationNotFoundError,
    LakeMigrationResumeError,
)
from src.engines.lake_migration.models import LakeMigrationDataset, LakeMigrationRun, LakeMigrationStatus
from src.engines.lake_migration.schemas import (
    LakeMigrationRunList,
    LakeMigrationRunRead,
    LakeMigrationStartRequest,
)
from src.engines.lake_migration.service import LakeMigrationService
from src.platform.lake.deps import get_lake_catalog
from src.platform.logs.deps import get_log_service

router = APIRouter(prefix='/lake-migrations', tags=['lake-migration'])

DependsDB = Depends(get_db)
DependsService = Depends(get_lake_migration_service)


@router.post('', status_code=202)
async def start_migration(
    body: LakeMigrationStartRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: AsyncSession = DependsDB,
    service: LakeMigrationService = DependsService,
    resume: Annotated[UUID | None, Query(description='Resume an existing run by id')] = None,
) -> JSONResponse:
    """Start (or resume) a PG → Iceberg migration.

    ``dataset`` can be ``'access_artifacts'``, ``'access_facts'``, or ``'all'``.
    When ``dataset='all'``, both datasets are migrated sequentially and the
    response is a list of two ``LakeMigrationRunRead`` objects.

    Query param ``?resume=<uuid>`` resumes an existing run.

    Returns HTTP 202 immediately; the actual migration runs in a background task.
    """
    correlation_id: str | None = getattr(request.state, 'correlation_id', None)
    catalog = get_lake_catalog(request)
    # Use test session factory if provided via app.state (test override pattern),
    # otherwise fall back to the production session factory.
    session_factory = getattr(request.app.state, 'session_factory_override', None) or get_session_factory()
    log_service = get_log_service(request)

    datasets_to_run: list[LakeMigrationDataset]
    runs: list[LakeMigrationRun] = []
    with translate_service_errors(
        {
            LakeMigrationConflictError: (409, lambda e: str(e)),
            LakeMigrationNotFoundError: (404, 'Lake migration run not found'),
            LakeMigrationDatasetError: (422, lambda e: str(e)),
            LakeMigrationResumeError: (409, lambda e: str(e)),
        }
    ):
        if body.dataset == 'all':
            datasets_to_run = [LakeMigrationDataset.access_artifacts, LakeMigrationDataset.access_facts]
        else:
            try:
                datasets_to_run = [LakeMigrationDataset(body.dataset)]
            except ValueError as exc:
                raise LakeMigrationDatasetError(
                    f"Unknown dataset: {body.dataset!r}. Use 'access_artifacts', 'access_facts', or 'all'."
                ) from exc

        for ds in datasets_to_run:
            run = await service.start_migration(
                session,
                dataset=ds,
                batch_size=body.batch_size,
                resume=resume if len(datasets_to_run) == 1 else None,
                correlation_id=correlation_id,
            )
            runs.append(run)

    await session.commit()

    # Schedule background migration for each run.
    for run in runs:
        background_tasks.add_task(
            _run_migration_background,
            session_factory=session_factory,
            run_id=run.id,
            dataset=run.dataset,
            catalog=catalog,
            log_service=log_service,
            correlation_id=correlation_id,
        )

    if len(runs) == 1:
        return JSONResponse(
            status_code=202,
            content=LakeMigrationRunRead.model_validate(runs[0]).model_dump(mode='json'),
        )
    return JSONResponse(
        status_code=202,
        content=[LakeMigrationRunRead.model_validate(r).model_dump(mode='json') for r in runs],
    )


@router.get('/{run_id}', response_model=LakeMigrationRunRead)
async def get_migration_run(
    run_id: UUID,
    session: AsyncSession = DependsDB,
    service: LakeMigrationService = DependsService,
) -> LakeMigrationRunRead:
    """Get a migration run by id."""
    run = await service.get_run(session, run_id)
    if run is None:
        from fastapi import HTTPException  # noqa: PLC0415

        raise HTTPException(status_code=404, detail='Lake migration run not found')
    return LakeMigrationRunRead.model_validate(run)


@router.get('', response_model=LakeMigrationRunList)
async def list_migration_runs(
    session: AsyncSession = DependsDB,
    service: LakeMigrationService = DependsService,
    status: Annotated[str | None, Query()] = None,
    dataset: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LakeMigrationRunList:
    """List migration runs with optional status/dataset filter and cursor pagination."""
    status_filter: LakeMigrationStatus | None = None
    if status is not None:
        try:
            status_filter = LakeMigrationStatus(status)
        except ValueError:
            pass

    dataset_filter: LakeMigrationDataset | None = None
    if dataset is not None:
        try:
            dataset_filter = LakeMigrationDataset(dataset)
        except ValueError:
            pass

    runs, next_cursor = await service.list_runs(
        session,
        status_filter=status_filter,
        dataset_filter=dataset_filter,
        limit=page_size,
        cursor=cursor,
    )
    return LakeMigrationRunList(
        items=[LakeMigrationRunRead.model_validate(r) for r in runs],
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------


async def _run_migration_background(
    *,
    session_factory: object,
    run_id: UUID,
    dataset: LakeMigrationDataset,
    catalog: object,
    log_service: object,
    correlation_id: str | None,
) -> None:
    """Execute the actual migration in a BackgroundTask using a fresh DB session.

    Opens a dedicated AsyncSession so the background task is not tied to the
    HTTP request's session (which may be committed/closed by the time this runs).
    """
    from src.engines.lake_migration.service import LakeMigrationService  # noqa: PLC0415
    from src.inventory.lake_batches.service import LakeBatchService  # noqa: PLC0415
    from src.platform.lake.config import LakeSettings  # noqa: PLC0415
    from src.platform.lake.duckdb_session import LakeSessionFactory  # noqa: PLC0415
    from src.platform.storage.factory import DataLakeStorageFactory  # noqa: PLC0415

    migration_svc = LakeMigrationService(
        log_service=log_service,  # type: ignore[arg-type]
        lake_batch_service=LakeBatchService(
            storage_factory=DataLakeStorageFactory(),
            log_service=log_service,  # type: ignore[arg-type]
        ),
    )

    # Build a lake session for Iceberg pre-write checks.
    settings = LakeSettings()
    lake_factory = LakeSessionFactory(settings=settings, log_service=log_service, pg_dsn=None)  # type: ignore[arg-type]
    lake_session = lake_factory.acquire()

    try:
        async with session_factory() as bg_session:  # type: ignore[operator]
            from sqlalchemy import select as _select  # noqa: PLC0415
            from src.engines.lake_migration.models import LakeMigrationRun as _Run  # noqa: PLC0415

            result = await bg_session.execute(_select(_Run).where(_Run.id == run_id))
            run = result.scalar_one_or_none()
            if run is None:
                return

            if dataset == LakeMigrationDataset.access_artifacts:
                await migration_svc.migrate_access_artifacts(
                    bg_session,
                    run,
                    lake_session=lake_session,
                    catalog=catalog,  # type: ignore[arg-type]
                    correlation_id=correlation_id,
                )
            else:
                await migration_svc.migrate_access_facts(
                    bg_session,
                    run,
                    lake_session=lake_session,
                    catalog=catalog,  # type: ignore[arg-type]
                    correlation_id=correlation_id,
                )
            await bg_session.commit()
    except Exception:  # noqa: BLE001
        # Background tasks must not raise — they run fire-and-forget.
        # Migration failure is recorded in PG by the service (status=failed).
        pass
    finally:
        lake_factory.release(lake_session)
