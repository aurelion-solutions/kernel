# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Adapter: converts a CartridgeManifest into a PolicyAssessmentRequest.

Lives in the engine layer so the dependency flows correctly:
  engines/policy_assessment → inventory/policy/cartridges  (downward, allowed)
"""

from __future__ import annotations

from typing import Any

from src.engines.policy_assessment.contracts import PolicyAssessmentRequest
from src.inventory.policy.cartridges.schemas import CartridgeManifest


def cartridge_manifest_to_request(
    manifest: CartridgeManifest,
    context: dict[str, Any],
) -> PolicyAssessmentRequest:
    return PolicyAssessmentRequest(
        policy_type=manifest.policy_type,
        assessment_strategy=manifest.assessment_strategy,
        policy_id=manifest.id,
        policy_definition=manifest.model_dump(),
        context=context,
    )
