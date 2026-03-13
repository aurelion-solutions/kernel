# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Secret API routes."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.secrets.deps import get_secret_service
from src.inventory.secrets.repository import create_secret_metadata, delete_secret_metadata, list_secrets
from src.inventory.secrets.schemas import SecretCreate, SecretRead
from src.inventory.secrets.service import SecretService
from src.platform.secrets.factory import UnsupportedProviderError

router = APIRouter(prefix='/secrets', tags=['secrets'])

DependsSecretService = Depends(get_secret_service)
DependsSession = Depends(get_db)


@router.get('', response_model=list[SecretRead])
async def list_(
    session: AsyncSession = DependsSession,
    provider: str | None = Query(None, description='Filter by provider'),
    namespace: str | None = Query(None, description='Filter by namespace'),
) -> list[SecretRead]:
    """List secret metadata (no values)."""
    secrets = await list_secrets(session, provider=provider, namespace=namespace)
    return [SecretRead(key=s.key, provider=s.provider, namespace=s.namespace) for s in secrets]


@router.post('', status_code=201)
async def create(
    body: SecretCreate,
    session: AsyncSession = DependsSession,
    service: SecretService = DependsSecretService,
) -> None:
    """Create a secret."""
    try:
        service.create_secret(
            key=body.key,
            provider=body.provider,
            namespace=body.namespace,
            value=body.value,
        )
        await create_secret_metadata(
            session,
            key=body.key,
            provider=body.provider,
            namespace=body.namespace,
        )
    except UnsupportedProviderError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=err.errors()) from err


@router.get('/{provider}/{key:path}', response_class=PlainTextResponse)
async def get(
    provider: str,
    key: str,
    namespace: str,
    service: SecretService = DependsSecretService,
) -> str:
    """Get a secret value."""
    try:
        return service.get_secret(key=key, provider=provider, namespace=namespace)
    except UnsupportedProviderError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except KeyError as err:
        raise HTTPException(status_code=404, detail='Secret not found') from err
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=err.errors()) from err


@router.delete('/{provider}/{key:path}', status_code=204)
async def delete(
    provider: str,
    key: str,
    namespace: str,
    session: AsyncSession = DependsSession,
    service: SecretService = DependsSecretService,
) -> None:
    """Delete a secret."""
    try:
        service.delete_secret(key=key, provider=provider, namespace=namespace)
        await delete_secret_metadata(
            session,
            key=key,
            provider=provider,
            namespace=namespace,
        )
    except UnsupportedProviderError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except KeyError as err:
        raise HTTPException(status_code=404, detail='Secret not found') from err
    except ValidationError as err:
        raise HTTPException(status_code=422, detail=err.errors()) from err
