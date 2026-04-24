# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SoD REST endpoints — /sod/* prefix."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from src.capabilities.access_analysis.services.capability_resolver import CapabilityResolverService
from src.capabilities.access_analysis.sod.deps import get_capability_resolver_service
from src.capabilities.access_analysis.sod.schemas import (
    ResolveCapabilitiesRequest,
    ResolveCapabilitiesResponse,
)

router = APIRouter(prefix='/sod', tags=['sod'])

DependsResolver = Depends(get_capability_resolver_service)


@router.post('/resolve-capabilities', response_model=ResolveCapabilitiesResponse)
async def resolve_capabilities(
    request: ResolveCapabilitiesRequest,
    service: CapabilityResolverService = DependsResolver,
) -> ResolveCapabilitiesResponse:
    """Pre-flight: which capability slugs would these sources grant?

    Never persists — pure read against active CapabilityMapping + Capability.
    Output: distinct, alphabetically sorted slug list.
    """
    slugs = await service.resolve_capabilities_for_sources(sources=request.sources)
    return ResolveCapabilitiesResponse(capability_slugs=slugs)
