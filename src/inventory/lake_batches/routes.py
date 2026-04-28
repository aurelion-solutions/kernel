# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.lake_batches.deps import get_lake_batch_service
from src.inventory.lake_batches.schemas import LakeBatchListResponse, LakeBatchRead, LakeBatchWriteRequest
from src.inventory.lake_batches.service import BatchNotFoundError, LakeBatchService
from src.platform.storage.factory import UnsupportedProviderError

router = APIRouter(prefix='/datalake/batches', tags=['datalake-batches'])
DependsSession = Depends(get_db)
DependsService = Depends(get_lake_batch_service)


_QueryLimit = Query(default=50, ge=1, le=200)
_QueryCursor = Query(default=None)


@router.get('', response_model=LakeBatchListResponse)
async def list_batches(
    limit: int = _QueryLimit,
    cursor: str | None = _QueryCursor,
    session: AsyncSession = DependsSession,
    service: LakeBatchService = DependsService,
) -> LakeBatchListResponse:
    """List lake batches (newest-first, keyset paginated).

    Uses opaque ``cursor`` token from a previous response ``next_cursor``.
    Returns 400 for malformed cursors.
    """
    try:
        batches, next_cursor = await service.list_batches(session, limit=limit, cursor=cursor)
    except ValueError:
        raise HTTPException(status_code=400, detail='Invalid cursor') from None

    return LakeBatchListResponse(
        items=[LakeBatchRead.model_validate(b) for b in batches],
        next_cursor=next_cursor,
    )


@router.post('', response_model=LakeBatchRead, status_code=201)
async def create_batch(
    request: LakeBatchWriteRequest,
    session: AsyncSession = DependsSession,
    service: LakeBatchService = DependsService,
) -> LakeBatchRead:
    """Create a lake batch: write records to lake, store metadata in PostgreSQL."""
    try:
        batch = await service.create_batch(
            session,
            storage_provider=request.storage_provider,
            dataset_type=request.dataset_type,
            records=request.records,
            task_id=request.task_id,
            application_id=request.application_id,
        )
    except UnsupportedProviderError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    await session.commit()
    return LakeBatchRead.model_validate(batch)


@router.get('/{batch_id}', response_model=LakeBatchRead)
async def get_batch(
    batch_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: LakeBatchService = DependsService,
) -> LakeBatchRead:
    """Get lake batch metadata by id."""
    batch = await service.get_batch(session, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail='Lake batch not found')
    return LakeBatchRead.model_validate(batch)


@router.get('/{batch_id}/data')
async def get_batch_data(
    batch_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: LakeBatchService = DependsService,
) -> list[dict]:
    """Get lake batch payload (records). Returns JSON array."""
    try:
        records = await service.read_batch(session, batch_id)
        return list(records)
    except BatchNotFoundError:
        raise HTTPException(status_code=404, detail='Lake batch not found') from None


@router.delete('/{batch_id}', status_code=204)
async def delete_batch(
    batch_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: LakeBatchService = DependsService,
) -> None:
    """Delete lake batch metadata and payload."""
    try:
        await service.delete_batch(session, batch_id, delete_payload=True)
    except BatchNotFoundError:
        raise HTTPException(status_code=404, detail='Lake batch not found') from None
    await session.commit()
