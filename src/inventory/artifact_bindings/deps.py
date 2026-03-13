# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding route dependencies."""

from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_artifact_binding_service() -> ArtifactBindingService:
    """Return ArtifactBindingService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return ArtifactBindingService(log_service=log_service)
