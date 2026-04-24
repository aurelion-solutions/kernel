# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP request/response schemas for SoD endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from src.capabilities.access_analysis.services import EffectiveGrantRef


class ResolveCapabilitiesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    sources: list[EffectiveGrantRef]


class ResolveCapabilitiesResponse(BaseModel):
    capability_slugs: list[str]  # sorted, distinct
