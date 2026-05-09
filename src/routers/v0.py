# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from fastapi import APIRouter
from src.routers._engines import include_engine_routers
from src.routers._inventory import include_inventory_routers
from src.routers._platform import include_platform_routers

router = APIRouter()

# Order: platform → inventory → engines (Layer 1 → cross-cutting state → Layer 2)
include_platform_routers(router)
include_inventory_routers(router)
include_engine_routers(router)

__all__ = ['router']
