# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from fastapi import APIRouter
from src.platform.applications.routes import router as applications_router
from src.platform.connectors.routes import router as connector_instances_router
from src.platform.events.routes import router as platform_events_router
from src.platform.lake.routes import router as lake_router
from src.platform.llm.routes import inference_router as llm_inference_router
from src.platform.llm.routes import models_router as llm_models_router
from src.platform.llm.routes import profiles_router as llm_execution_profiles_router
from src.platform.logs.buffer_recent_routes import router as platform_logs_router
from src.platform.logs.buffer_routes import router as log_buffer_router
from src.platform.logs.routes import router as logs_router
from src.platform.runtime_settings.routes import router as runtime_settings_router
from src.platform.secrets.provider_config.routes import router as secrets_providers_router


def include_platform_routers(router: APIRouter) -> None:
    """Register all platform-layer routers on the top-level APIRouter."""
    router.include_router(applications_router)
    router.include_router(lake_router)
    router.include_router(connector_instances_router)
    router.include_router(logs_router)
    router.include_router(log_buffer_router)
    router.include_router(platform_events_router)
    router.include_router(platform_logs_router)
    router.include_router(runtime_settings_router)
    router.include_router(secrets_providers_router)
    router.include_router(llm_models_router)
    router.include_router(llm_execution_profiles_router)
    router.include_router(llm_inference_router)
