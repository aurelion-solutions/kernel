# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure projector for the Effective Access Store.

This module is IO-free, DB-free, event-free, and clock-free.  All time data
must be supplied by the caller.  The ``project`` function is deterministic:
given the same ``(fact, initiative, now)`` triple it returns byte-identical output.

IMPORTANT — caller responsibilities (the #1 footgun for Step 3):
  - ``AccessFactView.subject_kind`` is NOT a column on the ``access_facts`` table.
    The caller must resolve it via ``JOIN subjects ON subjects.id = access_facts.subject_id``
    and pass the resolved value here.
  - ``AccessFactView.application_id`` is NOT a column on the ``access_facts`` table.
    The caller must resolve it via the resource's (or account's) ``application_id``
    and pass the resolved value here.

The projector does not fetch anything — it transforms DTOs into a draft row.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from src.capabilities.effective_access.models import EffectiveGrantEffect
from src.inventory.access_facts.schemas import AccessFactEffect
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind

# ---------------------------------------------------------------------------
# Input DTOs
# ---------------------------------------------------------------------------


class AccessFactView(BaseModel):
    """Flat view of an AccessFact row, enriched with caller-denormalized fields."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: UUID
    subject_id: UUID
    subject_kind: SubjectKind  # caller-denormalized from subjects.kind
    account_id: UUID | None
    application_id: UUID  # caller-denormalized from resources.application_id
    resource_id: UUID
    action: Action
    effect: AccessFactEffect
    valid_from: datetime  # tz-aware UTC expected; not validated at runtime
    valid_until: datetime | None


class InitiativeView(BaseModel):
    """Flat view of an Initiative row."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: UUID
    access_fact_id: UUID
    type: InitiativeType
    origin: str
    valid_from: datetime
    valid_until: datetime | None


# ---------------------------------------------------------------------------
# Output DTO
# ---------------------------------------------------------------------------


class EffectiveGrantDraft(BaseModel):
    """Pending effective-grant row ready for upsert by the Step 3 persistence layer.

    Absent by design: ``id`` (persistence mints UUIDs), ``created_at`` / ``updated_at``
    (ORM bookkeeping).
    """

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    subject_id: UUID
    subject_kind: SubjectKind
    application_id: UUID
    account_id: UUID | None
    resource_id: UUID
    action: Action
    effect: EffectiveGrantEffect
    initiative_type: InitiativeType
    initiative_origin: str
    valid_from: datetime
    valid_until: datetime | None
    source_access_fact_id: UUID
    source_initiative_id: UUID
    observed_at: datetime
    tombstoned_at: datetime | None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_EFFECT_MAP: dict[AccessFactEffect, EffectiveGrantEffect] = {
    AccessFactEffect.allow: EffectiveGrantEffect.allow,
    AccessFactEffect.deny: EffectiveGrantEffect.deny,
}


def _min_optional(a: datetime | None, b: datetime | None) -> datetime | None:
    """Return the earlier of two optional datetimes, treating None as +infinity."""
    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


# ---------------------------------------------------------------------------
# Core projection function
# ---------------------------------------------------------------------------


def project(
    fact: AccessFactView,
    initiative: InitiativeView,
    *,
    now: datetime,
) -> list[EffectiveGrantDraft]:
    """Map a (fact, initiative) pair to a list of effective-grant drafts.

    Returns a single-element list.  Raises ``ValueError`` if the pair is invalid.
    Does not read a clock, write to DB, emit events, or generate UUIDs.
    """
    if initiative.access_fact_id != fact.id:
        raise ValueError(
            f'initiative does not belong to the given access fact '
            f'(initiative.access_fact_id={initiative.access_fact_id!r}, fact.id={fact.id!r})'
        )

    effect = _EFFECT_MAP[fact.effect]  # KeyError on unknown value is intentional

    valid_from = max(fact.valid_from, initiative.valid_from)
    valid_until = _min_optional(fact.valid_until, initiative.valid_until)
    tombstoned_at = now if (valid_until is not None and valid_until < valid_from) else None

    draft = EffectiveGrantDraft(
        subject_id=fact.subject_id,
        subject_kind=fact.subject_kind,
        application_id=fact.application_id,
        account_id=fact.account_id,
        resource_id=fact.resource_id,
        action=fact.action,
        effect=effect,
        initiative_type=initiative.type,
        initiative_origin=initiative.origin,
        valid_from=valid_from,
        valid_until=valid_until,
        source_access_fact_id=fact.id,
        source_initiative_id=initiative.id,
        observed_at=now,
        tombstoned_at=tombstoned_at,
    )
    return [draft]
