# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation capability — public re-exports."""

from src.engines.inventory_reconcile.contracts import (
    Handler,
    HandlerAlreadyRegisteredError,
    NormalizationResult,
)
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.engines.inventory_reconcile.registry import (
    get_handler,
    list_registered_types,
    register_handler,
)
from src.engines.inventory_reconcile.schemas import ReconciliationRunSummary
from src.engines.inventory_reconcile.service import ReconciliationService

__all__ = [
    'Handler',
    'HandlerAlreadyRegisteredError',
    'NormalizationResult',
    'ReconciliationDeltaItem',
    'ReconciliationDeltaItemStatus',
    'ReconciliationDeltaOperation',
    'ReconciliationRun',
    'ReconciliationRunStatus',
    'ReconciliationRunSummary',
    'ReconciliationService',
    'get_handler',
    'list_registered_types',
    'register_handler',
]

# Side-effect import: registers inventory_reconcile actions in ACTION_REGISTRY at import time.
from src.engines.inventory_reconcile import actions as _actions  # noqa: F401, E402
