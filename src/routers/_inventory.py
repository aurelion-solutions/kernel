# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from fastapi import APIRouter
from src.inventory.access_artifacts.routes import router as access_artifacts_router
from src.inventory.access_facts.routes import router as access_facts_router
from src.inventory.access_model.capabilities.routes import router as capabilities_router
from src.inventory.access_model.capability_grants.routes import (
    router as capability_grants_router,
)
from src.inventory.access_model.capability_mappings.routes import (
    router as capability_mappings_router,
)
from src.inventory.access_model.capability_scope_keys.routes import (
    router as capability_scope_keys_router,
)
from src.inventory.access_usage_facts.routes import router as access_usage_facts_router
from src.inventory.accounts.routes import router as accounts_router
from src.inventory.actions.routes import router as actions_router
from src.inventory.artifact_bindings.routes import router as artifact_bindings_router
from src.inventory.assessment.feedbacks.routes import router as feedbacks_router
from src.inventory.assessment.findings.routes import router as findings_router
from src.inventory.assessment.mitigation_controls.routes import (
    router as mitigation_controls_router,
)
from src.inventory.assessment.mitigations.routes import router as mitigations_router
from src.inventory.assessment.scan_runs.routes import router as scan_runs_router
from src.inventory.customers.routes import router as customers_router
from src.inventory.employee_records.routes import router as employee_records_router
from src.inventory.employees.routes import router as employees_router
from src.inventory.initiatives.routes import router as initiatives_router
from src.inventory.lake_batches.routes import router as lake_batches_router
from src.inventory.nhi.routes import router as nhi_router
from src.inventory.org_units.routes import router as org_units_router
from src.inventory.ownership_assignments.routes import router as ownership_assignments_router
from src.inventory.persons.routes import router as persons_router
from src.inventory.policy.catalog.routes import router as policy_catalog_router
from src.inventory.policy.sod_rule_conditions.routes import (
    router as sod_rule_conditions_router,
)
from src.inventory.policy.sod_rules.routes import router as sod_rules_router
from src.inventory.resources.routes import router as resources_router
from src.inventory.secrets.routes import router as secrets_router
from src.inventory.subjects.routes import router as subjects_router
from src.inventory.threat_facts.routes import router as threat_facts_router


def include_inventory_routers(router: APIRouter) -> None:
    """Register all inventory-layer routers on the top-level APIRouter."""
    router.include_router(capabilities_router)
    router.include_router(capability_scope_keys_router)
    router.include_router(capability_mappings_router)
    router.include_router(capability_grants_router)
    router.include_router(sod_rules_router)
    router.include_router(sod_rule_conditions_router)
    router.include_router(policy_catalog_router)
    router.include_router(scan_runs_router)
    router.include_router(findings_router)
    router.include_router(mitigation_controls_router)
    router.include_router(mitigations_router)
    router.include_router(feedbacks_router)
    router.include_router(persons_router)
    router.include_router(accounts_router)
    router.include_router(actions_router)
    router.include_router(customers_router)
    router.include_router(subjects_router)
    router.include_router(resources_router)
    router.include_router(access_artifacts_router)
    router.include_router(access_facts_router)
    router.include_router(artifact_bindings_router)
    router.include_router(initiatives_router)
    router.include_router(ownership_assignments_router)
    router.include_router(access_usage_facts_router)
    router.include_router(threat_facts_router)
    router.include_router(employees_router)
    router.include_router(employee_records_router)
    router.include_router(nhi_router)
    router.include_router(org_units_router)
    router.include_router(lake_batches_router)
    router.include_router(secrets_router)
