# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake infrastructure routes (Layer 1)."""

from fastapi import APIRouter, Depends, HTTPException, status
from pyiceberg.catalog import Catalog
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_catalog, get_lake_settings
from src.platform.lake.exceptions import LakeMaintenanceError
from src.platform.lake.read_schemas import LakeCompactionRequest, LakeCompactionResponse, LakeStatusResponse
from src.platform.lake.service import get_lake_status, run_compaction
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

router = APIRouter(prefix='/lake', tags=['lake'])

DependsCatalog = Depends(get_lake_catalog)
DependsSettings = Depends(get_lake_settings)
DependsLogService = Depends(get_log_service)
DependsDb = Depends(get_db)


@router.get('/status', response_model=LakeStatusResponse)
def read_lake_status(
    catalog: Catalog = DependsCatalog,
    settings: LakeSettings = DependsSettings,
    log_service: LogService = DependsLogService,
) -> LakeStatusResponse:
    """Return current catalog URI, warehouse URI, storage provider, and per-table snapshot metadata."""
    return get_lake_status(catalog, settings, log_service=log_service)


@router.post('/compaction', response_model=LakeCompactionResponse)
async def trigger_lake_compaction(
    body: LakeCompactionRequest,
    catalog: Catalog = DependsCatalog,
    settings: LakeSettings = DependsSettings,
    session: AsyncSession = DependsDb,
    log_service: LogService = DependsLogService,
) -> LakeCompactionResponse:
    """Run compaction, snapshot expiry, and optionally orphan cleanup for one or all lake tables.

    The safety gate skips ``clean_orphan_files`` when an active Sync/Apply run is detected
    or when recent ingest batches lie within the ``2 × orphan_older_than_hours`` window.
    """
    try:
        return await run_compaction(
            catalog,
            settings,
            session,
            table=body.table,
            retention_days=body.retention_days,
            orphan_older_than_hours=body.orphan_older_than_hours,
            target_file_size_mb=body.target_file_size_mb,
            log_service=log_service,
        )
    except LakeMaintenanceError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc
