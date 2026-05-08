# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-based cartridge evaluation service.

Wires FileCartridgeLoader → cartridge_manifest_to_request → PolicyAssessmentDispatcher
into a single evaluate_file(path, context) call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.engines.policy_assessment.cartridge_adapter import cartridge_manifest_to_request
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput
from src.engines.policy_assessment.dispatcher import PolicyAssessmentDispatcher
from src.engines.policy_assessment.service import PolicyService
from src.inventory.policy.cartridges.loader import FileCartridgeLoader


class PolicyCartridgeAssessmentService:
    """Evaluate a single cartridge YAML file through the dispatcher.

    Args:
        loader: cartridge file loader; defaults to FileCartridgeLoader().
        dispatcher: assessment dispatcher; defaults to one backed by a default PolicyService.
    """

    def __init__(
        self,
        loader: FileCartridgeLoader | None = None,
        dispatcher: PolicyAssessmentDispatcher | None = None,
    ) -> None:
        self._loader = loader if loader is not None else FileCartridgeLoader()
        self._dispatcher = (
            dispatcher if dispatcher is not None else PolicyAssessmentDispatcher(policy_service=PolicyService())
        )

    def evaluate_file(self, path: Path, context: dict[str, Any]) -> PolicyAssessmentOutput:
        manifest = self._loader.load_file(path)
        request = cartridge_manifest_to_request(manifest, context)
        return self._dispatcher.evaluate(request)
