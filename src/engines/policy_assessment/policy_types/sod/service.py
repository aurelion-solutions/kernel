# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodEvaluatorService — read-only service that wraps DB reads around the pure evaluator.

Never calls session.flush(), session.commit(), or emits events.
The route handler owns the transaction (per ARCH_CONTEXT "Transaction ownership").
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.policy_assessment.policy_types.sod.evaluator import CapabilityGrantView, Violation, evaluate
from src.engines.policy_assessment.policy_types.sod.exceptions import (
    WhatIfApplicationNotFoundError,
    WhatIfCapabilityNotFoundError,
    WhatIfScopeKeyNotFoundError,
    WhatIfScopeValueMismatchError,
)
from src.engines.policy_assessment.policy_types.sod.repository import (
    application_exists,
    load_capability_id_and_slug,
    load_enabled_rules,
    load_subject_capability_grants,
    load_subject_mitigations,
    scope_key_exists,
)
from src.engines.policy_assessment.policy_types.sod.schemas import CapabilityGrantOverride

# Sentinel values for synthetic what-if override fields.
# Negative IDs cannot collide with real autoincrement CapabilityGrant.id values.
_SYNTHETIC_SOURCE_EG_ID = UUID('00000000-0000-0000-0000-000000000000')
_SYNTHETIC_SOURCE_MAPPING_ID = 0


class SodEvaluatorService:
    """Read-only service: loads DB inputs, calls pure evaluator, returns Violation list.

    No LogService, no EventService — this is a read-only evaluation path.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def evaluate_subject(
        self,
        subject_id: UUID,
        at: datetime,
    ) -> list[Violation]:
        """Evaluate all active SoD rules for a subject at point-in-time ``at``.

        Steps:
          1. Load enabled SodRules with conditions + M2M capability_ids (two queries).
          2. Load subject's active CapabilityGrants at ``at`` with capability slug + EAS join.
          3. Load subject's active/proposed Mitigations valid at ``at``.
          4. Call pure evaluate() and return its output unchanged.

        Never persists. Returns [] if subject has no capabilities or no enabled rules.
        A nonexistent subject_id also returns [] — no existence check performed.
        """
        rules = await load_enabled_rules(self._session)
        if not rules:
            return []

        grants = await load_subject_capability_grants(self._session, subject_id, at)
        mitigations = await load_subject_mitigations(self._session, subject_id, at)

        return evaluate(
            subject_id=subject_id,
            capability_grants=grants,
            rules=rules,
            mitigations=mitigations,
            at=at,
        )

    async def what_if_subject(
        self,
        subject_id: UUID,
        at: datetime,
        capability_overrides: list[CapabilityGrantOverride],
    ) -> list[Violation]:
        """Evaluate SoD rules for a subject with synthetic capability overrides.

        Loads the same DB inputs as evaluate_subject, converts overrides to
        CapabilityGrantView objects, and calls the pure evaluate() with capability_overrides.

        Never persists, never emits. Read-only path.

        Raises WhatIfCapabilityNotFoundError  if capability_id is not found.
        Raises WhatIfScopeKeyNotFoundError    if scope_key_id is not found.
        Raises WhatIfApplicationNotFoundError if application_id is not found.
        Raises WhatIfScopeValueMismatchError  if scope_value / GLOBAL mismatch.
        (WhatIfScopeValueInvalidError is raised at schema validation time, before this method.)
        """
        rules = await load_enabled_rules(self._session)
        if not rules:
            return []

        grants = await load_subject_capability_grants(self._session, subject_id, at)
        mitigations = await load_subject_mitigations(self._session, subject_id, at)

        override_views = await _build_override_views(
            self._session,
            subject_id=subject_id,
            overrides=capability_overrides,
        )

        return evaluate(
            subject_id=subject_id,
            capability_grants=grants,
            rules=rules,
            mitigations=mitigations,
            capability_overrides=override_views,
            at=at,
        )


# ---------------------------------------------------------------------------
# Module-level helpers (per Phase 11 Step 3 service-layer discipline)
# ---------------------------------------------------------------------------


async def _validate_override_references(
    session: AsyncSession,
    override: CapabilityGrantOverride,
) -> tuple[int, str]:
    """Validate that an override's FK references exist and scope_value is consistent.

    Returns (capability_id, capability_slug) on success.

    Raises:
        WhatIfCapabilityNotFoundError   — capability_id not found
        WhatIfScopeKeyNotFoundError     — scope_key_id not found
        WhatIfApplicationNotFoundError  — application_id not found
        WhatIfScopeValueMismatchError   — scope_value vs. GLOBAL key mismatch
    """
    cap = await load_capability_id_and_slug(session, override.capability_id)
    if cap is None:
        raise WhatIfCapabilityNotFoundError(override.capability_id)

    exists, is_global = await scope_key_exists(session, override.scope_key_id)
    if not exists:
        raise WhatIfScopeKeyNotFoundError(override.scope_key_id)

    # Scope value must be None iff scope key is GLOBAL
    if is_global and override.scope_value is not None:
        raise WhatIfScopeValueMismatchError(override.scope_key_id, override.scope_value)
    if not is_global and override.scope_value is None:
        raise WhatIfScopeValueMismatchError(override.scope_key_id, override.scope_value)

    if not await application_exists(session, override.application_id):
        raise WhatIfApplicationNotFoundError(override.application_id)

    return cap


def _override_to_view(
    override: CapabilityGrantOverride,
    subject_id: UUID,
    capability_id: int,
    capability_slug: str,
    index: int,
) -> CapabilityGrantView:
    """Convert a validated CapabilityGrantOverride to a CapabilityGrantView.

    Synthetic fields:
    - id: negative sentinel (-1, -2, …) — cannot collide with real autoincrement ids
    - source_effective_grant_id: zero UUID sentinel (documented in TASK.md §4)
    - source_capability_mapping_id: 0
    - source_access_fact_ids / source_initiative_ids: [] (empty — what-if violations never persist)
    """
    return CapabilityGrantView(
        id=-(index + 1),
        subject_id=subject_id,
        capability_id=capability_id,
        capability_slug=capability_slug,
        scope_key_id=override.scope_key_id,
        scope_value=override.scope_value,
        application_id=override.application_id,
        source_effective_grant_id=_SYNTHETIC_SOURCE_EG_ID,
        source_capability_mapping_id=_SYNTHETIC_SOURCE_MAPPING_ID,
        source_access_fact_ids=[],
        source_initiative_ids=[],
    )


async def _build_override_views(
    session: AsyncSession,
    subject_id: UUID,
    overrides: list[CapabilityGrantOverride],
) -> list[CapabilityGrantView]:
    """Validate and convert all overrides to CapabilityGrantView objects.

    Validation is done per override in order; first failing override aborts the whole request.
    """
    views: list[CapabilityGrantView] = []
    for idx, override in enumerate(overrides):
        cap_id, cap_slug = await _validate_override_references(session, override)
        views.append(
            _override_to_view(
                override,
                subject_id=subject_id,
                capability_id=cap_id,
                capability_slug=cap_slug,
                index=idx,
            )
        )
    return views
