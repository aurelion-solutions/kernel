# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation capability — public re-exports."""

from src.capabilities.reconciliation.contracts import (
    Handler,
    HandlerAlreadyRegisteredError,
    NormalizationResult,
)
from src.capabilities.reconciliation.registry import (
    get_handler,
    list_registered_types,
    register_handler,
)
from src.capabilities.reconciliation.schemas import ReconciliationRunSummary
from src.capabilities.reconciliation.service import ReconciliationService

__all__ = [
    'Handler',
    'HandlerAlreadyRegisteredError',
    'NormalizationResult',
    'ReconciliationRunSummary',
    'ReconciliationService',
    'get_handler',
    'list_registered_types',
    'register_handler',
]
