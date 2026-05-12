# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from fastapi import APIRouter
from src.engines.access_analysis.analytics.routes import router as analytics_router
from src.engines.access_analysis.assessment_preview.routes import (
    router as orphan_detector_router,
)
from src.engines.access_analysis.capability_preview.routes import (
    router as capability_preview_router,
)
from src.engines.access_analysis.reports.routes import router as reports_router
from src.engines.access_analysis.scan_routes import router as scan_execution_router
from src.engines.access_apply.routes import router as access_apply_router
from src.engines.access_effective.routes import router as effective_grants_router
from src.engines.access_plan.routes import router as access_plan_router
from src.engines.ingest.routes import router as connector_results_router
from src.engines.inventory_reconcile.routes import router as inventory_reconcile_router
from src.engines.inventory_sync.routes import router as inventory_sync_router
from src.engines.policy_assessment.policy_types.sod.routes import (
    router as sod_evaluator_router,
)
from src.engines.policy_assessment.routes import router as policy_router


def include_engine_routers(router: APIRouter) -> None:
    """Register all engine-layer routers on the top-level APIRouter."""
    router.include_router(analytics_router)
    router.include_router(reports_router)
    router.include_router(access_apply_router)
    router.include_router(inventory_reconcile_router)
    router.include_router(inventory_sync_router)
    router.include_router(effective_grants_router)
    router.include_router(capability_preview_router)
    router.include_router(sod_evaluator_router)
    router.include_router(orphan_detector_router)
    router.include_router(scan_execution_router)
    router.include_router(connector_results_router)
    router.include_router(policy_router)
    router.include_router(access_plan_router)
