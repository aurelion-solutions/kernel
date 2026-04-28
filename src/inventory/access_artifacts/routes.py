# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

# DECISION: The spec proposed POST /access-artifacts:bulk (colon-suffix RFC-style).
# FastAPI/Starlette do route literal `:` characters; however, httpx TestClient used
# in the test suite percent-encodes `:` in path segments, so Starlette returns 404.
# Fallback applied: POST /access-artifacts/bulk and POST /access-artifacts/bulk-tombstone.
# The GET endpoints remain on /access-artifacts (no colon path needed).

"""AccessArtifact API routes — read + bulk write (Iceberg-only, Phase 15 Step 16)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from src.core.http.errors import translate_service_errors
from src.inventory.access_artifacts.deps import get_access_artifact_service
from src.inventory.access_artifacts.schemas import (
    AccessArtifactBulkTombstoneRequest,
    AccessArtifactBulkTombstoneResponse,
    AccessArtifactBulkUpsertRequest,
    AccessArtifactBulkUpsertResponse,
    AccessArtifactCursorPage,
    AccessArtifactRead,
    AccessArtifactView,
)
from src.inventory.access_artifacts.service import AccessArtifactBatchItem as BatchItem
from src.inventory.access_artifacts.service import (
    AccessArtifactBatchTooLargeError,
    AccessArtifactLakeNotConfiguredError,
    AccessArtifactLakeWriteError,
    AccessArtifactService,
    ArtifactCursorPage,
    InvalidCursorError,
)
from src.platform.lake.deps import get_lake_session, get_optional_lake_session
from src.platform.lake.duckdb_session import LakeSession

router = APIRouter(prefix='/access-artifacts', tags=['access-artifacts'])
DependsService = Depends(get_access_artifact_service)
DependsLakeSession = Depends(get_optional_lake_session)
DependsMandatoryLakeSession = Depends(get_lake_session)

_BULK_ERROR_MAP = {
    AccessArtifactBatchTooLargeError: (422, 'batch size exceeds limit'),
    AccessArtifactLakeNotConfiguredError: (503, 'lake backend not configured'),
    AccessArtifactLakeWriteError: (502, 'lake write failed'),
}


@router.post('/bulk', response_model=AccessArtifactBulkUpsertResponse)
async def bulk_upsert_access_artifacts(
    body: AccessArtifactBulkUpsertRequest,
    service: AccessArtifactService = DependsService,
) -> AccessArtifactBulkUpsertResponse:
    """Bulk upsert access artifacts (up to 10 000 items per request)."""
    items = [
        BatchItem(
            application_id=item.application_id,
            artifact_type=item.artifact_type,
            external_id=item.external_id,
            payload=item.payload,
            raw_name=item.raw_name,
            effect=item.effect,
            valid_from=item.valid_from,
            valid_until=item.valid_until,
            observed_at=item.observed_at,
        )
        for item in body.items
    ]
    with translate_service_errors(_BULK_ERROR_MAP):
        result = await service.upsert_batch(
            items,
            ingest_batch_id=body.ingest_batch_id,
            correlation_id=body.correlation_id,
        )
    return AccessArtifactBulkUpsertResponse(
        row_count=result.row_count,
        snapshot_id=result.snapshot_id,
        backend=result.backend,
    )


@router.post('/bulk-tombstone', response_model=AccessArtifactBulkTombstoneResponse)
async def bulk_tombstone_access_artifacts(
    body: AccessArtifactBulkTombstoneRequest,
    service: AccessArtifactService = DependsService,
) -> AccessArtifactBulkTombstoneResponse:
    """Bulk tombstone access artifacts by ID."""
    with translate_service_errors(_BULK_ERROR_MAP):
        result = await service.tombstone_batch(
            artifact_ids=body.artifact_ids,
            observed_at=body.observed_at,
            correlation_id=body.correlation_id,
        )
    return AccessArtifactBulkTombstoneResponse(
        tombstoned_count=result.row_count,
        snapshot_id=result.snapshot_id,
        backend=result.backend,
    )


@router.get('', response_model=AccessArtifactCursorPage)
async def list_access_artifacts(
    application_id: uuid.UUID | None = None,
    artifact_type: str | None = None,
    is_active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = None,
    service: AccessArtifactService = DependsService,
    lake_session: LakeSession | None = DependsLakeSession,
) -> AccessArtifactCursorPage:
    """List access artifacts with optional filters (cursor-paginated via Iceberg)."""
    warehouse_uri = service._get_warehouse_uri()
    with translate_service_errors({InvalidCursorError: (400, 'invalid cursor')}):
        page: ArtifactCursorPage = await service.list_artifacts_iceberg(
            lake_session,
            warehouse_uri=warehouse_uri,
            application_id=application_id,
            artifact_type=artifact_type,
            is_active=is_active,
            cursor=cursor,
            page_size=service._get_read_page_size(),
        )
    return AccessArtifactCursorPage(items=page.items, next_cursor=page.next_cursor)


@router.get('/{artifact_id}', response_model=AccessArtifactRead)
async def get_access_artifact(
    artifact_id: uuid.UUID,
    service: AccessArtifactService = DependsService,
    lake_session: LakeSession = DependsMandatoryLakeSession,
) -> AccessArtifactRead:
    """Get access artifact by id."""
    from fastapi import HTTPException

    view: AccessArtifactView | None = await service.get_artifact(lake_session, artifact_id)
    if view is None:
        raise HTTPException(status_code=404, detail='Access artifact not found')
    return AccessArtifactRead.model_validate(view.model_dump())
