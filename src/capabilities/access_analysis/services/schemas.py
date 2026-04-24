# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared DTOs for access_analysis services.

EffectiveGrantRef is reused by CapabilityResolverService (Step 5) and the
what-if SoD endpoint (Step 16). Placing it here, not inside capability_resolver.py,
avoids a future forced move when Step 16 needs to import it.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EffectiveGrantRef(BaseModel):
    """Caller-built EffectiveGrant-shaped reference.

    May represent a real EAS row OR a hypothetical in-memory construction.
    The resolver does NOT round-trip to the DB — it never reads effective_grants.

    Omissions by design:
    - ``subject_id`` is omitted — slug resolution does not depend on subject.
    - ``resource_attributes`` / ``subject_attributes`` are omitted — those feed
      scope_value resolution, which is irrelevant for slug-only output.
    - ``tombstoned_at`` is omitted — resolver treats every supplied source as live.
      Filtering tombstoned rows is the caller's responsibility.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    application_id: UUID
    resource_id: UUID
    action_slug: str  # e.g. 'read', 'write', 'execute', 'admin'
    # Caller-denormalized resource fields — required because the projector's
    # resource_kind / resource_path_glob matchers read them directly.
    # The caller supplies them; the resolver never touches Resource rows.
    resource_kind: str
    resource_external_id: str
