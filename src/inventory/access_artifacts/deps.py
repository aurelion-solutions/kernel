# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact route dependencies.

Phase 15 Step 16: event_service removed. lake_catalog is mandatory (iceberg-only).
"""

from fastapi import Request
from src.inventory.access_artifacts.service import (
    AccessArtifactLakeNotConfiguredError,
    AccessArtifactService,
)


def get_access_artifact_service(request: Request) -> AccessArtifactService:
    """Return AccessArtifactService with injected lake deps.

    Raises RuntimeError if lake_catalog is not initialised — fails fast at
    request time rather than silently corrupting state.
    """
    lake_catalog = getattr(request.app.state, 'lake_catalog', None)
    if lake_catalog is None:
        raise AccessArtifactLakeNotConfiguredError()
    lake_settings = getattr(request.app.state, 'lake_settings', None)
    log_service = getattr(request.app.state, 'log_service', None)
    return AccessArtifactService(
        log_service=log_service,
        lake_catalog=lake_catalog,
        lake_settings=lake_settings,
    )
