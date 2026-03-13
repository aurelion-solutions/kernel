# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from typing import Literal

from pydantic import BaseModel, Field


class ConnectorRegistrationMessage(BaseModel):
    event_type: Literal['connector.registered', 'connector.heartbeat']
    instance_id: str = Field(min_length=1, max_length=255)
    tags: list[str] = Field(default_factory=list)
