# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation capability — public re-exports."""

from src.engines.reconciliation.contracts import (
    Handler,
    HandlerAlreadyRegisteredError,
    NormalizationResult,
)
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.engines.reconciliation.registry import (
    get_handler,
    list_registered_types,
    register_handler,
)
from src.engines.reconciliation.schemas import ReconciliationRunSummary
from src.engines.reconciliation.service import ReconciliationService

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
