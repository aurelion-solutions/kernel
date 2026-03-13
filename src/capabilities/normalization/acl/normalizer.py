# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure ACL entry normalizer — no DB, no I/O."""

from __future__ import annotations

from src.capabilities.normalization.acl.schemas import ACLEntryPayload, NormalizedAccess
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.enums import Action
from src.inventory.resources.models import ResourceDataSensitivity, ResourceEnvironment, ResourcePrivilegeLevel

# Locked verb → (Action, ResourcePrivilegeLevel) mapping for Phase 08.
_VERB_MAP: dict[str, tuple[Action, ResourcePrivilegeLevel]] = {
    'read': (Action.read, ResourcePrivilegeLevel.read),
    'write': (Action.write, ResourcePrivilegeLevel.write),
    'admin': (Action.administer, ResourcePrivilegeLevel.admin),
}


class ACLPayloadError(ValueError):
    """Raised when the ACL payload cannot be normalized."""


def normalize_acl_entry(payload: ACLEntryPayload) -> NormalizedAccess:
    """Map an ACLEntryPayload to NormalizedAccess.

    Raises ACLPayloadError for unknown verbs or invalid resource_external_id.
    This is the single place where ACL source vocabulary crosses into closed
    inventory vocabulary.
    """
    if not payload.resource_external_id.strip():
        raise ACLPayloadError(f'resource_external_id must not be empty, got: {payload.resource_external_id!r}')

    if payload.verb not in _VERB_MAP:
        raise ACLPayloadError(f'Unknown ACL verb: {payload.verb!r}')

    action, privilege_level = _VERB_MAP[payload.verb]

    effect = AccessFactEffect(payload.effect)

    environment: ResourceEnvironment | None = (
        ResourceEnvironment(payload.environment) if payload.environment is not None else None
    )
    data_sensitivity: ResourceDataSensitivity | None = (
        ResourceDataSensitivity(payload.data_sensitivity) if payload.data_sensitivity is not None else None
    )

    return NormalizedAccess(
        resource_external_id=payload.resource_external_id,
        resource_kind=payload.resource_kind,
        action=action,
        effect=effect,
        privilege_level=privilege_level,
        environment=environment,
        data_sensitivity=data_sensitivity,
    )
