# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the capability_preview slice.

Actions registered here wrap CapabilityResolverService without touching
service.py, routes.py, or any other existing file. Registration happens at
import time via @register_action decorator side effects.
"""

from __future__ import annotations

from src.engines.access_analysis.capability_preview.schemas import (
    ResolveCapabilitiesRequest,
    ResolveCapabilitiesResponse,
)
from src.engines.access_analysis.services.capability_resolver import CapabilityResolverService
from src.platform.orchestrator.registry import ActionContext, register_action


@register_action(  # type: ignore[arg-type]
    engine='access_analysis.capability_preview',
    action='resolve',
    args_schema=ResolveCapabilitiesRequest,
    result_schema=ResolveCapabilitiesResponse,
    idempotent=True,
)
async def capability_preview_resolve(
    args: ResolveCapabilitiesRequest,
    ctx: ActionContext,
) -> ResolveCapabilitiesResponse:
    """Pre-flight: which capability slugs would these sources grant?"""
    service = CapabilityResolverService(ctx.session)
    slugs = await service.resolve_capabilities_for_sources(sources=args.sources)
    return ResolveCapabilitiesResponse(capability_slugs=slugs)
