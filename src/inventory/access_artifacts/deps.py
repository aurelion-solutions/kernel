# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact route dependencies."""

from src.inventory.access_artifacts.service import AccessArtifactService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_access_artifact_service() -> AccessArtifactService:
    """Return AccessArtifactService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return AccessArtifactService(log_service=log_service)
