# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from src.platform.connectors.models import ConnectorInstance


class ConnectorInstanceResponse(BaseModel):
    id: str
    instance_id: str
    tags: list[str]
    is_online: bool
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_instance(cls, instance: ConnectorInstance) -> ConnectorInstanceResponse:
        return cls(
            id=str(instance.id),
            instance_id=instance.instance_id,
            tags=instance.tags,
            is_online=instance.is_online,
            last_seen_at=instance.last_seen_at,
            created_at=instance.created_at,
            updated_at=instance.updated_at,
        )
