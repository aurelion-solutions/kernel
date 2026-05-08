# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic schemas for the ACL normalizer capability."""

from __future__ import annotations

from typing import Literal
import uuid

from pydantic import BaseModel, field_validator
from src.inventory.access_facts.schemas import AccessFactEffect
from src.inventory.enums import Action
from src.inventory.resources.models import ResourceDataSensitivity, ResourceEnvironment, ResourcePrivilegeLevel


class ACLEntryPayload(BaseModel):
    """One ACL line from the source."""

    resource_external_id: str
    resource_kind: str
    verb: Literal['read', 'write', 'admin']
    effect: Literal['allow', 'deny']
    environment: Literal['production', 'staging', 'dev'] | None = None
    data_sensitivity: Literal['pii', 'financial', 'public'] | None = None

    @field_validator('resource_external_id')
    @classmethod
    def resource_external_id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError('resource_external_id must not be empty')
        return v


class NormalizedAccess(BaseModel):
    """Intermediate tuple produced by normalize_acl_entry, consumed by ACLNormalizerService."""

    resource_external_id: str
    resource_kind: str
    action: Action
    effect: AccessFactEffect
    privilege_level: ResourcePrivilegeLevel | None
    environment: ResourceEnvironment | None
    data_sensitivity: ResourceDataSensitivity | None


class NormalizationResult(BaseModel):
    """Result of a single ingest_and_normalize call."""

    artifact_id: uuid.UUID
    resource_id: uuid.UUID
    access_fact_id: uuid.UUID
    binding_id: uuid.UUID
    created_fact: bool
    created_resource: bool
