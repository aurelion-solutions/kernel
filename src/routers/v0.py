# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from fastapi import APIRouter
from src.capabilities.ingest.routes import router as connector_results_router
from src.capabilities.policy.routes import router as policy_router
from src.capabilities.provisioning.routes import router as provisioning_router
from src.capabilities.reconciliation.routes import router as reconciliation_router
from src.inventory.access_artifacts.routes import router as access_artifacts_router
from src.inventory.access_facts.routes import router as access_facts_router
from src.inventory.access_usage_facts.routes import router as access_usage_facts_router
from src.inventory.accounts.routes import router as accounts_router
from src.inventory.artifact_bindings.routes import router as artifact_bindings_router
from src.inventory.customers.routes import router as customers_router
from src.inventory.employee_records.routes import router as employee_records_router
from src.inventory.employees.routes import router as employees_router
from src.inventory.initiatives.routes import router as initiatives_router
from src.inventory.lake_batches.routes import router as lake_batches_router
from src.inventory.nhi.routes import router as nhi_router
from src.inventory.ownership_assignments.routes import router as ownership_assignments_router
from src.inventory.persons.routes import router as persons_router
from src.inventory.resources.routes import router as resources_router
from src.inventory.secrets.routes import router as secrets_router
from src.inventory.subjects.routes import router as subjects_router
from src.inventory.threat_facts.routes import router as threat_facts_router
from src.platform.applications.routes import router as applications_router
from src.platform.connectors.routes import router as connector_instances_router
from src.platform.logs.buffer_routes import router as log_buffer_router
from src.platform.logs.routes import router as logs_router
from src.platform.secrets.provider_config.routes import router as secrets_providers_router

router = APIRouter()
router.include_router(applications_router)
router.include_router(provisioning_router)
router.include_router(reconciliation_router)
router.include_router(persons_router)
router.include_router(accounts_router)
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
router.include_router(connector_results_router)
router.include_router(lake_batches_router)
router.include_router(connector_instances_router)
router.include_router(logs_router)
router.include_router(log_buffer_router)
router.include_router(secrets_providers_router)
router.include_router(secrets_router)
router.include_router(policy_router)
