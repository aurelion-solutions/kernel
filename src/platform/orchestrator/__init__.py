# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform orchestrator — public re-exports."""

from src.platform.orchestrator.loader import (
    PipelineActionRefError,
    PipelineDefinition,
    PipelineDefinitionLoader,
    PipelineLoadError,
    PipelineRequiresOrderError,
    PipelineSchemaError,
    PipelineTemplatingError,
    PipelineTriggerError,
)
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    ActionNotFoundError,
    ActionRegistryError,
    ActionRegistry,
    ActionResultValidationError,
    DuplicateActionError,
    RegisteredAction,
    register_action,
)
from src.platform.orchestrator.beat import (
    BeatScheduleParseError,
    BeatTickResult,
    already_fired_in_window,
    beat_loop,
    beat_tick,
    compute_previous_fire_point,
    fire_schedule,
)
from src.platform.orchestrator.routes import router, well_known_router
from src.platform.orchestrator.schemas import (
    ActionDescriptor,
    CreatePipelineRunRequest,
    CreatePipelineRunResponse,
    PipelineDetail,
    PipelineRunDetail,
    PipelineRunSummary,
    PipelineSummary,
    PipelineTriggerSpec,
    StepRunDetail,
    StepRunSummary,
)
from src.platform.orchestrator.service import PipelineOrchestratorService, compute_content_hash
from src.platform.orchestrator.service_types import (
    OrchestratorRowMissing,
    OrchestratorStateConflict,
    PipelineRunCreateResult,
    ReclaimResult,
)

__all__ = [
    'ACTION_REGISTRY',
    'BeatScheduleParseError',
    'BeatTickResult',
    'already_fired_in_window',
    'beat_loop',
    'beat_tick',
    'compute_previous_fire_point',
    'fire_schedule',
    'ActionArgsValidationError',
    'ActionContext',
    'ActionDescriptor',
    'ActionNotFoundError',
    'ActionRegistryError',
    'ActionRegistry',
    'ActionResultValidationError',
    'CreatePipelineRunRequest',
    'CreatePipelineRunResponse',
    'DuplicateActionError',
    'OrchestratorRowMissing',
    'OrchestratorStateConflict',
    'PipelineActionRefError',
    'PipelineDefinition',
    'PipelineDefinitionLoader',
    'PipelineDetail',
    'PipelineLoadError',
    'PipelineOrchestratorService',
    'PipelineRequiresOrderError',
    'PipelineRunCreateResult',
    'PipelineRunDetail',
    'PipelineRunSummary',
    'PipelineSchemaError',
    'PipelineSummary',
    'PipelineTemplatingError',
    'PipelineTriggerError',
    'PipelineTriggerSpec',
    'ReclaimResult',
    'RegisteredAction',
    'StepRunDetail',
    'StepRunSummary',
    'compute_content_hash',
    'register_action',
    'router',
    'well_known_router',
]
