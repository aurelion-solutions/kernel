# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependency providers for the access_plan engine routes (Phase 19 Step E1).

Wires:
- AccessPlanService (DB session + PDP service + EventService + RuntimeSettingsConfig)
- PipelineOrchestratorService (for POST /plans/{id}/apply)
- Pipeline definitions dict (for POST /plans/{id}/apply pipeline lookup)
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.access_plan.service import AccessPlanService
from src.engines.policy_assessment.generative.service import GenerativePDPService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.orchestrator.deps import get_orchestrator_service
from src.platform.orchestrator.loader import PipelineDefinitionLoader
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

_PIPELINES_DIR = Path(__file__).parent.parent.parent.parent / 'pipelines'

_DependsDB = Depends(get_db)


def _build_event_service() -> EventService:
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'noop')
    sink = event_sink_factory.get(provider)
    return EventService(sink=sink)


def get_access_plan_service(
    session: AsyncSession = _DependsDB,
) -> AccessPlanService:
    """Return an AccessPlanService bound to the request session.

    Uses a noop GenerativePDPService when no rule_pack is configured —
    the PDP is stateless over its input and does not require runtime setup.
    """
    pdp = GenerativePDPService()
    event_service = _build_event_service()
    settings = RuntimeSettingsConfig()
    return AccessPlanService(
        session=session,
        pdp_service=pdp,
        event_service=event_service,
        settings=settings,
    )


async def get_plan_orchestrator_service(
    request: Request,
    session: AsyncSession = _DependsDB,
) -> PipelineOrchestratorService:
    """Return a PipelineOrchestratorService for POST /plans/{id}/apply."""
    return await get_orchestrator_service(request=request, session=session)


def get_plan_pipelines(request: Request) -> dict:
    """Return the loaded pipeline definitions dict from app.state.

    On first access, calls loader.load_dir(<repo>/pipelines) and stores the
    result on app.state.pipelines. Subsequent requests return the cached dict.
    """
    if not hasattr(request.app.state, 'pipelines'):
        if not hasattr(request.app.state, 'pipeline_loader'):
            request.app.state.pipeline_loader = PipelineDefinitionLoader()
        loader = request.app.state.pipeline_loader
        request.app.state.pipelines = loader.load_dir(_PIPELINES_DIR)
    return request.app.state.pipelines  # type: ignore[no-any-return]
