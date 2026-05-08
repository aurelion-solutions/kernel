# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the policy catalog slice."""

from __future__ import annotations

from fastapi import Request
from src.inventory.policy.catalog.service import PolicyCatalogService


def get_policy_catalog_service(request: Request) -> PolicyCatalogService:
    """Return PolicyCatalogService with injected LogService from app state."""
    log_service = getattr(request.app.state, 'log_service', None)
    return PolicyCatalogService(log_service=log_service)
