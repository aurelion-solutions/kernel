# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Secret route dependencies."""

from fastapi import Request
from src.inventory.secrets.service import SecretService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService
from src.platform.secrets.factory import secret_manager_factory


async def get_secret_service(request: Request) -> SecretService:
    """Return SecretService from app state or default."""
    service = getattr(request.app.state, 'secret_service', None)
    if service is None:
        log_service = LogService(factory=log_sink_factory)
        service = SecretService(factory=secret_manager_factory, log_service=log_service)
        request.app.state.secret_service = service
    return service
