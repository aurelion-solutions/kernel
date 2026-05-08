# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure capability projector — matches EffectiveGrants against CapabilityMappings.

This module is IO-free, DB-free, event-free, clock-free, and random-free.
All time data must be supplied by the caller via the ``now`` parameter.
``project_grant`` is deterministic: given the same inputs it returns byte-identical output.

Scope value NULL semantics:
- NULL scope_value is the GLOBAL sentinel, not a missing value.
- When mapping.scope_key_id == global_scope_key_id, the projector forces scope_value=None
  regardless of what the source resolves to. This keeps the projector independent of
  seeded ids — the caller supplies global_scope_key_id.

Subject attribute source:
- subject_attribute kind silently returns None if the attribute is not present in the payload.
  This is forward-compatible stub behaviour — subject attributes are a future-phase concept.
  # TODO: wire subject attribute store when available (future phase)
  # For now, subject attributes are not a first-class store → returns None

Scope value normalization:
- Trimmed, lowercased, truncated to 255 chars.
- Hard truncation may collide two distinct long source values into the same scope_value.
  Acceptable for IGA-scale scope keys (operator-side concern).

Glob matching:
- Uses fnmatch.fnmatchcase (case-sensitive). Operator-typed patterns are case-meaningful.

Forbidden in this module: print, logging, LogService, datetime.now, uuid.uuid4,
any asyncpg/sqlalchemy import, any DB session call.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import fnmatch
from uuid import UUID

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Input DTOs
# ---------------------------------------------------------------------------


class EffectiveGrantView(BaseModel):
    """Caller-built view of an EffectiveGrant row + denormalized resource fields."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: UUID
    subject_id: UUID
    application_id: UUID
    resource_id: UUID
    action_slug: str  # mapped from grant.action enum via .value
    tombstoned_at: datetime | None
    # Caller-denormalized from JOIN with resources:
    resource_kind: str
    resource_external_id: str
    # Optional payloads for scope_value resolution:
    resource_attributes: dict[str, str]  # key → value; empty dict if none
    subject_attributes: dict[str, str]  # key → value; empty dict if none


class CapabilityMappingView(BaseModel):
    """Active mapping data the projector matches against."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: int
    capability_id: int
    application_id: UUID | None
    resource_id: UUID | None
    resource_kind: str | None
    resource_path_glob: str | None
    action_slug: str | None
    scope_key_id: int
    scope_value_source: dict  # raw JSONB dict — projector dispatches on 'kind'
    is_active: bool  # caller filters; projector double-checks defensively


# ---------------------------------------------------------------------------
# Output DTO
# ---------------------------------------------------------------------------


class CapabilityGrantDraft(BaseModel):
    """Pending capability-grant row ready for upsert.

    Absent by design: id (DB autoincrement), no created_at (we use observed_at).
    """

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    subject_id: UUID
    capability_id: int
    scope_key_id: int
    scope_value: str | None  # NULL for GLOBAL; normalized otherwise
    application_id: UUID  # denormalized from EG; immutable post-projection
    source_effective_grant_id: UUID
    source_capability_mapping_id: int
    observed_at: datetime
    tombstoned_at: datetime | None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_scope_value(raw: str | None) -> str | None:
    """Normalize scope_value: strip, lowercase, truncate to 255.

    Returns None if input is None or empty after strip.
    Hard truncation at 255 chars — column is String(255). Trade-off: truncation
    may collide two distinct source values into the same scope_value.
    Acceptable for IGA-scale scope keys.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return stripped.lower()[:255]


def matcher_applies(grant: EffectiveGrantView, mapping: CapabilityMappingView) -> bool:
    """Check if a mapping applies to a grant. Three-stage matcher, short-circuits on first non-match.

    Stage 1: application_id filter.
    Stage 2: action_slug filter.
    Stage 3: resource match (XOR — exactly one of resource_id, resource_kind, resource_path_glob).

    Public — used by ``project_grant`` and by ``CapabilityResolverService.resolve_capabilities_for_sources``.
    Changing the signature is a contract change.
    """
    # Stage 1: application_id filter
    if mapping.application_id is not None and grant.application_id != mapping.application_id:
        return False

    # Stage 2: action_slug filter
    if mapping.action_slug is not None and grant.action_slug != mapping.action_slug:
        return False

    # Stage 3: resource match (XOR)
    if mapping.resource_id is not None:
        return grant.resource_id == mapping.resource_id

    if mapping.resource_kind is not None:
        return grant.resource_kind == mapping.resource_kind

    if mapping.resource_path_glob is not None:
        return fnmatch.fnmatchcase(grant.resource_external_id, mapping.resource_path_glob)

    # All three resource match fields are None — corrupted mapping (violates DB CHECK).
    raise ValueError(
        f'CapabilityMapping id={mapping.id} has no resource_id, resource_kind, '
        f'or resource_path_glob set — violates ck_capability_mappings_resource_match_xor'
    )


def _resolve_scope_value(grant: EffectiveGrantView, mapping: CapabilityMappingView) -> str | None:
    """Resolve the scope_value from the mapping's scope_value_source discriminated union.

    Dispatches on source['kind']:
    - 'subject_attribute': grant.subject_attributes.get(source['key']) → None if missing
    - 'resource_attribute': grant.resource_attributes.get(source['key']) → None if missing
    - 'application_id': str(grant.application_id)
    - 'constant': source['value']
    - Unknown kind: raises ValueError (corrupted mapping data)

    # TODO: wire subject attribute store when available (future phase)
    # For now, subject attributes are not a first-class store → returns None

    Result is passed through _normalize_scope_value.
    """
    source = mapping.scope_value_source
    kind = source.get('kind')

    if kind == 'subject_attribute':
        # TODO: wire subject attribute store when available (future phase)
        # For now, subject attributes are not a first-class store → returns None
        raw = grant.subject_attributes.get(source['key'])
        return _normalize_scope_value(raw)

    if kind == 'resource_attribute':
        raw = grant.resource_attributes.get(source['key'])
        return _normalize_scope_value(raw)

    if kind == 'application_id':
        return _normalize_scope_value(str(grant.application_id))

    if kind == 'constant':
        return _normalize_scope_value(source['value'])

    raise ValueError(f'Unknown scope_value_source kind={kind!r} in mapping id={mapping.id} — corrupted data')


# ---------------------------------------------------------------------------
# Core projection function
# ---------------------------------------------------------------------------


def project_grant(
    grant: EffectiveGrantView,
    active_mappings: Sequence[CapabilityMappingView],
    *,
    now: datetime,
    global_scope_key_id: int,
) -> list[CapabilityGrantDraft]:
    """Project one EffectiveGrant against a set of active CapabilityMappings.

    For each matching mapping, produces a CapabilityGrantDraft. Returns empty list
    if no mappings matched.

    The output list is sorted deterministically by
    (source_capability_mapping_id, capability_id, scope_key_id, scope_value or '',
     source_effective_grant_id) so two calls with the same inputs produce the same order.

    Tombstoning mirrors EAS: if grant.tombstoned_at is set, all produced drafts
    have tombstoned_at == grant.tombstoned_at.

    global_scope_key_id: when mapping.scope_key_id == global_scope_key_id, scope_value
    is forced to None (GLOBAL sentinel) regardless of what the source resolves to.
    This keeps the projector independent of seeded ids.
    """
    drafts: list[CapabilityGrantDraft] = []

    for mapping in active_mappings:
        # Defensive — caller should pre-filter active mappings, but double-check.
        if not mapping.is_active:
            continue

        if not matcher_applies(grant, mapping):
            continue

        scope_value = _resolve_scope_value(grant, mapping)

        # Force GLOBAL sentinel regardless of source resolution.
        if mapping.scope_key_id == global_scope_key_id:
            scope_value = None

        # Mirror EAS tombstoning — if source EG is tombstoned, derived row is too.
        tombstoned_at = grant.tombstoned_at

        drafts.append(
            CapabilityGrantDraft(
                subject_id=grant.subject_id,
                capability_id=mapping.capability_id,
                scope_key_id=mapping.scope_key_id,
                scope_value=scope_value,
                application_id=grant.application_id,
                source_effective_grant_id=grant.id,
                source_capability_mapping_id=mapping.id,
                observed_at=now,
                tombstoned_at=tombstoned_at,
            )
        )

    # Sort deterministically.
    drafts.sort(
        key=lambda d: (
            d.source_capability_mapping_id,
            d.capability_id,
            d.scope_key_id,
            d.scope_value or '',
            str(d.source_effective_grant_id),
        )
    )

    return drafts
