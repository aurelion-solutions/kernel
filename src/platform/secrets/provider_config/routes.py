# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Provider API routes."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.secrets.factory import secret_manager_factory
from src.platform.secrets.provider_config.repository import get_by_name, list_providers
from src.platform.secrets.provider_config.schemas import ProviderCreate, ProviderRead
from src.platform.secrets.provider_config.service import (
    BUILTIN_PROVIDERS,
    create_provider_and_register,
    delete_provider_and_unregister,
)

router = APIRouter(prefix='/secrets/providers', tags=['providers'])

DependsSession = Depends(get_db)


@router.get('', response_model=list[ProviderRead])
async def list_(
    session: AsyncSession = DependsSession,
) -> list[ProviderRead]:
    """List all providers (built-in + custom)."""
    custom = await list_providers(session)
    all_names = secret_manager_factory.list_names()
    custom_names = {p.name for p in custom}
    result = []
    for p in custom:
        result.append(ProviderRead(id=str(p.id), name=p.name, type=p.type, config=p.config))
    for name in all_names:
        if name not in custom_names:
            result.append(ProviderRead(id='', name=name, type=name, config={}))
    result.sort(key=lambda x: x.name)
    return result


@router.get('/{name}', response_model=ProviderRead)
async def get(
    name: str,
    session: AsyncSession = DependsSession,
) -> ProviderRead:
    """Get a provider by name."""
    provider = await get_by_name(session, name)
    if provider is not None:
        return ProviderRead(id=str(provider.id), name=provider.name, type=provider.type, config=provider.config)
    if name in secret_manager_factory.list_names():
        return ProviderRead(id='', name=name, type=name, config={})
    raise HTTPException(status_code=404, detail='Provider not found')


@router.post('', response_model=ProviderRead, status_code=201)
async def create(
    body: ProviderCreate,
    session: AsyncSession = DependsSession,
) -> ProviderRead:
    """Create a custom provider."""
    try:
        provider = await create_provider_and_register(session, name=body.name, type=body.type, config=body.config)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    await session.commit()
    return ProviderRead(
        id=str(provider.id),
        name=provider.name,
        type=provider.type,
        config=provider.config,
    )


@router.delete('/{name}', status_code=204)
async def delete(
    name: str,
    session: AsyncSession = DependsSession,
) -> None:
    """Delete a custom provider."""
    deleted = await delete_provider_and_unregister(session, name)
    if not deleted:
        if name in BUILTIN_PROVIDERS:
            raise HTTPException(status_code=400, detail=f'Cannot delete built-in provider: {name!r}')
        raise HTTPException(status_code=404, detail='Provider not found')
    await session.commit()
