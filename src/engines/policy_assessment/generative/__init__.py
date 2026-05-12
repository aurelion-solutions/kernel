# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Generative PDP sub-package.

Stateless desired-state projection:
  GenerativePDPService.assess(...) → list[ProjectedFact]

This is a parallel method to the reactive PDP — existing service.py is
untouched.  The caller (access_plan in a later step) supplies the full
snapshot; this module never reads the database.
"""

from src.engines.policy_assessment.generative.schemas import (
    InitiativeProjection,
    ProjectedFact,
    SubjectContext,
)
from src.engines.policy_assessment.generative.service import GenerativePDPService

__all__ = [
    'GenerativePDPService',
    'InitiativeProjection',
    'ProjectedFact',
    'SubjectContext',
]
