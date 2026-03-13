# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Log read API routes."""

import os

from fastapi import APIRouter, HTTPException
from src.platform.logs.read_factory import UnsupportedReadProviderError, log_read_factory

router = APIRouter(prefix='/logs', tags=['logs'])


def _get_provider() -> str:
    """Resolve provider name from env. Default is 'file'."""
    return os.environ.get('AURELION_LOG_PROVIDER', 'file')


@router.get('')
async def read_logs(limit: int = 100) -> list[dict]:
    """Read recent log records. Resolves read provider from AURELION_LOG_PROVIDER."""
    if limit < 1 or limit > 10_000:
        raise HTTPException(status_code=400, detail='limit must be between 1 and 10000')
    provider = _get_provider()
    try:
        reader = log_read_factory.get(provider)
    except UnsupportedReadProviderError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    try:
        records = reader.read(limit=limit)
        return records
    except NotImplementedError as err:
        raise HTTPException(status_code=501, detail=str(err)) from err
