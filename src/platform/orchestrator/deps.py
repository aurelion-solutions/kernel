# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the pipeline orchestrator routes (Step 11).

Design notes
------------
- ``get_pipeline_loader`` resolves a singleton from ``app.state.pipeline_loader``
  (lazily created). The loader is stateless except for the lazy-compiled
  jsonschema validator; re-building it per request wastes ~5 ms on the first call.
  Hot-reload (Step 14) will mutate ``app.state.pipelines``, not the loader.

- ``get_loaded_pipelines`` returns a ``dict[str, PipelineDefinition]`` from
  ``app.state.pipelines``. On first access, calls ``loader.load_dir(<repo>/pipelines)``.
  In tests this stays empty unless the test seeds it explicitly — endpoint tests
  insert a fake ``PipelineDefinition`` into ``app.state.pipelines`` rather than
  dropping YAML files.

- ``get_orchestrator_service`` wires PipelineOrchestratorService with session,
  EventService, and LogService from app state.

Single-version-per-name invariant: the loader keys by name only. If Step 14
introduces multi-version loading, the loader cache key and version-default logic
in routes.py must both change. Cost of reversal: ~20 LOC.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.logs.deps import get_log_service
from src.platform.orchestrator.cartridge_paths import PIPELINE_SOURCE_DIRS as _PIPELINE_SOURCE_DIRS
from src.platform.orchestrator.loader import PipelineDefinition, PipelineDefinitionLoader
from src.platform.orchestrator.service import PipelineOrchestratorService

if TYPE_CHECKING:
    from src.platform.logs.service import LogService, NoOpLogService


def _build_event_service() -> EventService:
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'noop')
    sink = event_sink_factory.get(provider)
    return EventService(sink=sink)


def get_pipeline_loader(request: Request) -> PipelineDefinitionLoader:
    """Return the singleton PipelineDefinitionLoader from app.state.

    Created lazily on first request so that test apps without a lifespan work
    without explicit setup.
    """
    if not hasattr(request.app.state, 'pipeline_loader'):
        request.app.state.pipeline_loader = PipelineDefinitionLoader()
    return request.app.state.pipeline_loader  # type: ignore[no-any-return]


def get_loaded_pipelines(request: Request) -> dict[str, PipelineDefinition]:
    """Return the cached pipeline definitions dict from app.state.

    On first access, calls loader.load_many(_PIPELINE_SOURCE_DIRS) — which
    scans the kernel-shipped ``pipelines/`` directory and the monorepo-root
    ``cartridges/journey/`` directory — and stores the merged result on
    app.state.pipelines. Subsequent requests return the cached dict.

    In tests: stays empty (``{}``) unless the test explicitly seeds
    ``app.state.pipelines`` with fake PipelineDefinition instances.
    """
    if not hasattr(request.app.state, 'pipelines'):
        loader = get_pipeline_loader(request)
        request.app.state.pipelines = loader.load_many(_PIPELINE_SOURCE_DIRS)
    return request.app.state.pipelines  # type: ignore[no-any-return]


_DependsDB = Depends(get_db)


async def get_orchestrator_service(
    request: Request,
    session: AsyncSession = _DependsDB,
) -> PipelineOrchestratorService:
    """Build PipelineOrchestratorService with all dependencies wired via FastAPI DI."""
    log_service: LogService | NoOpLogService = get_log_service(request)
    event_service = _build_event_service()
    return PipelineOrchestratorService(
        session=session,
        events=event_service,
        logs=log_service,
    )
