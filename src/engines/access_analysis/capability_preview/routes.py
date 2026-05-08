# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Capability preview endpoints.

  POST /capability-preview/resolve  — pre-flight: which capability slugs would
                                      these sources grant?

Never persists — pure read against active CapabilityMapping + Capability.
Output: distinct, alphabetically sorted slug list.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from src.engines.access_analysis.capability_preview.deps import get_capability_resolver_service
from src.engines.access_analysis.capability_preview.schemas import (
    ResolveCapabilitiesRequest,
    ResolveCapabilitiesResponse,
)
from src.engines.access_analysis.services.capability_resolver import CapabilityResolverService

router = APIRouter(prefix='/capability-preview', tags=['capability-preview'])

DependsResolver = Depends(get_capability_resolver_service)


@router.post('/resolve', response_model=ResolveCapabilitiesResponse)
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
