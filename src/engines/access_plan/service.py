# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessPlanService — declarative access planning (D2: planning logic).

Responsibilities:
1. Collect subject context (employee or NHI) from inventory.
2. Collect current_facts (effective grants) from access_effective.
3. Collect current_initiatives from inventory.initiatives.
4. Call GenerativePDPService.assess() — stateless desired-state projection.
5. Diff desired vs effective → list of PlanItem candidates with abstract kinds:
   add_fact / remove_fact / modify_fact.
6. For each diff item, populate:
   - add_fact / modify_fact: initiatives (from PDP decision)
   - remove_fact: initiative_refs (UUIDs of existing initiatives covering the fact)
   - always: decision_snapshot (immutable copy of PDP Decision)
7. Compute content_hash from the diff result.
8. Idempotency: reuse plan on idempotency_key hit; hash-based dedup within 5s window.
9. Auto-supersedes: mark older active plans superseded.
10. Safe-revoke check: if remove_fact items exceed threshold → requires_confirmation=True.
11. Advisory lock via pg_advisory_xact_lock on subject_ref for consistent supersedes.

Session discipline: flush, never commit (per ARCH_CONTEXT Transaction ownership).
Events: access_plan.created emitted post-flush via EventService.

D3 (account status reasoning) converts abstract kinds to concrete operation kinds.
D4 (DAG resolver) builds PlanDependency rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Any
import uuid
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_effective.models import EffectiveGrant
from src.engines.access_plan.dag_resolver import DAGResult, resolve_dag
from src.engines.access_plan.models import AccessPlan, AccessPlanStatus, PlanItem, PlanItemKind
from src.engines.access_plan.repository import (
    count_current_effective_grants,
    fetch_account_status_for_subject,
    fetch_connector_descriptor,
    fetch_connector_transitions,
    fetch_current_effective_grants,
    fetch_current_initiatives_for_subject,
    fetch_employee_context_data,
    fetch_nhi_context_data,
    fetch_subject_principal_ids,
    find_active_plan_for_subject,
    find_plan_by_idempotency_key,
    find_recent_active_plan_by_content_hash,
    insert_plan_dependencies,
    insert_plan_items,
    resolve_subject_kind,
    supersede_older_active_plans,
)
from src.engines.access_plan.status_resolver import (
    NOT_EXISTS,
    resolve_item_kind,
)
from src.engines.policy_assessment.generative.schemas import (
    CurrentFact,
    CurrentInitiative,
    ProjectedFact,
    SubjectContext,
)
from src.engines.policy_assessment.generative.service import GenerativePDPService
from src.inventory.initiatives.models import Initiative
from src.inventory.subjects.models import SubjectKind
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

_COMPONENT = 'engines.access_plan'

# Abstract change kinds (D2 output — D3 maps to concrete operation kinds)
_ABSTRACT_ADD = 'add_fact'
_ABSTRACT_REMOVE = 'remove_fact'
_ABSTRACT_MODIFY = 'modify_fact'

# Default placeholder PlanItemKind used for abstract items before D3 resolves them.
# Stored in PlanItem.kind as a sentinel; D3 will overwrite via DAG-resolution step.
# Using grant_role as a stable placeholder (always valid enum value).
_PLACEHOLDER_KIND = PlanItemKind.grant_role


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class SubjectNotFoundError(Exception):
    """Raised when the subject_ref does not resolve to a known subject."""

    def __init__(self, subject_ref: str) -> None:
        self.subject_ref = subject_ref
        super().__init__(f'Subject not found: {subject_ref}')


class SubjectContextNotFoundError(Exception):
    """Raised when inventory record for subject cannot be located."""

    def __init__(self, subject_ref: str, subject_type: str) -> None:
        self.subject_ref = subject_ref
        self.subject_type = subject_type
        super().__init__(f'{subject_type} context not found for subject_ref={subject_ref}')


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------


def _fact_key(application: str, target_descriptor: dict[str, Any]) -> str:
    """Stable dedup key for (application, target_descriptor) pairs."""
    sorted_items = sorted(target_descriptor.items())
    descriptor_str = ','.join(f'{k}={v}' for k, v in sorted_items)
    return f'{application}::{descriptor_str}'


def _grant_key(grant: EffectiveGrant) -> str:
    """Key for an effective grant based on application_id + resource_id."""
    # We use application_id + resource_id as effective fact key.
    # target_descriptor is not directly on EffectiveGrant, so we use resource_id as proxy.
    return f'{grant.application_id}::{grant.resource_id}'


def _projected_fact_key(pf: ProjectedFact) -> str:
    return _fact_key(pf.application, pf.target_descriptor)


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def _compute_content_hash(
    diff_items: list[dict[str, Any]],
) -> str:
    """Compute SHA-256 of the ordered diff result.

    Hash is computed over the serialised list of diff items, sorted by
    (abstract_kind, application, target_descriptor) for determinism.
    """
    sorted_items = sorted(
        diff_items,
        key=lambda d: (
            d.get('abstract_kind', ''),
            d.get('application', ''),
            json.dumps(d.get('target_descriptor', {}), sort_keys=True),
        ),
    )
    payload = json.dumps(sorted_items, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# PDP Decision serialization
# ---------------------------------------------------------------------------


def _decision_to_dict(decision: Any) -> dict[str, Any]:
    """Serialize a PDP Decision to a plain dict for decision_snapshot."""
    if hasattr(decision, 'model_dump'):
        return decision.model_dump(mode='json')  # type: ignore[no-any-return]
    return dict(decision)


# ---------------------------------------------------------------------------
# Build PlanItem from diff
# ---------------------------------------------------------------------------


def _build_add_fact_item(
    plan_id: UUID,
    projected_fact: ProjectedFact,
) -> tuple[PlanItem, dict[str, Any]]:
    """Build a PlanItem for an add_fact operation."""
    decision_dict = _decision_to_dict(projected_fact.decision)
    initiatives = [
        {
            'type': init.type.value,
            'origin': init.origin,
            'valid_from': init.valid_from.isoformat() if init.valid_from else None,
            'valid_until': init.valid_until.isoformat() if init.valid_until else None,
            'source_initiative_id': str(init.source_initiative_id) if init.source_initiative_id else None,
        }
        for init in projected_fact.initiatives
    ]
    policy_rule_refs = [r.rule_id for r in projected_fact.decision.reasons if hasattr(r, 'rule_id') and r.rule_id]
    item = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan_id,
        kind=_PLACEHOLDER_KIND,
        application=projected_fact.application,
        target_descriptor=dict(projected_fact.target_descriptor),
        initiatives=initiatives,
        initiative_refs=[],
        policy_rule_refs=policy_rule_refs,
        decision_snapshot=decision_dict,
    )
    diff_descriptor = {
        'abstract_kind': _ABSTRACT_ADD,
        'application': projected_fact.application,
        'target_descriptor': projected_fact.target_descriptor,
        'policy_rule_refs': policy_rule_refs,
        'initiatives': [i['type'] for i in initiatives],
    }
    return item, diff_descriptor


def _build_remove_fact_item(
    plan_id: UUID,
    application: str,
    target_descriptor: dict[str, Any],
    covering_initiatives: list[Initiative],
    decision_dict: dict[str, Any],
) -> tuple[PlanItem, dict[str, Any]]:
    """Build a PlanItem for a remove_fact operation."""
    initiative_refs = [str(init.id) for init in covering_initiatives]
    item = PlanItem(
        id=uuid.uuid4(),
        plan_id=plan_id,
        kind=_PLACEHOLDER_KIND,
        application=application,
        target_descriptor=dict(target_descriptor),
        initiatives=[],
        initiative_refs=initiative_refs,
        policy_rule_refs=[],
        decision_snapshot=decision_dict,
    )
    diff_descriptor = {
        'abstract_kind': _ABSTRACT_REMOVE,
        'application': application,
        'target_descriptor': target_descriptor,
        'initiative_refs': initiative_refs,
    }
    return item, diff_descriptor


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------


def _build_plan_created_event(
    plan: AccessPlan,
    items_count: int,
    superseded_count: int,
    correlation_id: UUID,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='access_plan.plan.created',
        occurred_at=datetime.now(UTC),
        correlation_id=str(correlation_id),
        causation_id=None,
        payload={
            'plan_id': str(plan.id),
            'subject_ref': plan.subject_ref,
            'subject_type': plan.subject_type,
            'status': plan.status.value,
            'items_count': items_count,
            'superseded_count': superseded_count,
            'requires_confirmation': plan.requires_confirmation,
            'content_hash': plan.content_hash,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=plan.subject_ref,
    )


# ---------------------------------------------------------------------------
# AccessPlanService
# ---------------------------------------------------------------------------


class AccessPlanService:
    """Plans access changes via diff(desired, current) with idempotency and supersedes.

    Args:
        session: Caller-owned AsyncSession. Service flushes, never commits.
        pdp_service: GenerativePDPService (stateless, pre-constructed with RulePack).
        event_service: EventService for domain events.
        settings: RuntimeSettingsConfig for safe_revoke_threshold.
    """

    def __init__(
        self,
        session: AsyncSession,
        pdp_service: GenerativePDPService,
        event_service: EventService | None = None,
        settings: RuntimeSettingsConfig | None = None,
    ) -> None:
        self._session = session
        self._pdp = pdp_service
        self._events = event_service if event_service is not None else noop_event_service
        self._settings = settings if settings is not None else RuntimeSettingsConfig()

    async def create_plan(
        self,
        *,
        subject_ref: str,
        idempotency_key: str | None = None,
        context_overrides: dict[str, Any] | None = None,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> AccessPlan:
        """Create (or reuse) an access plan for the given subject.

        Algorithm:
        1. Acquire pg_advisory_xact_lock(subject_ref) for consistent supersedes.
        2. If idempotency_key provided and existing plan found → return it (reuse).
        3. Resolve subject kind from subjects table.
        4. Build SubjectContext from inventory (employee or NHI).
        5. Fetch current_facts (effective grants) and current_initiatives.
        6. Call PDP.assess() → desired list[ProjectedFact].
        7. Diff desired vs effective → plan items.
        8. Compute content_hash; dedup within 5s window if no idempotency_key.
        9. Check safe-revoke threshold.
        10. Persist AccessPlan + PlanItems, supersede older active plans.
        11. Emit access_plan.created event.

        Returns the AccessPlan (new or reused).
        """
        effective_now = now if now is not None else datetime.now(UTC)
        corr_id = correlation_id if correlation_id is not None else uuid.uuid4()

        # Step 1: advisory lock on subject_ref (transaction-scoped, auto-released)
        await self._acquire_subject_lock(subject_ref)

        # Step 2: idempotency_key reuse
        if idempotency_key is not None:
            existing = await find_plan_by_idempotency_key(self._session, idempotency_key)
            if existing is not None:
                return existing

        # Step 3: resolve subject kind
        subject_kind = await resolve_subject_kind(self._session, subject_ref)
        if subject_kind is None:
            raise SubjectNotFoundError(subject_ref)

        subject_uuid = UUID(subject_ref)

        # Step 4: build SubjectContext
        subject_context = await self._build_subject_context(subject_ref, subject_kind)

        # Step 5: fetch current state
        current_grants = await fetch_current_effective_grants(self._session, subject_uuid, effective_now)
        current_initiatives_raw = await fetch_current_initiatives_for_subject(
            self._session, subject_uuid, effective_now
        )

        # Convert to PDP types
        current_facts = _grants_to_current_facts(current_grants)
        current_initiatives = _initiatives_to_current_initiatives(current_initiatives_raw)

        # Step 6: PDP desired state
        desired = self._pdp.assess(
            subject_context,
            current_facts,
            current_initiatives,
            context_overrides=context_overrides,
            now=effective_now,
        )

        # Step 7: diff
        items_data, diff_descriptors = _compute_diff(
            plan_id=uuid.uuid4(),  # temp, replaced below
            desired=desired,
            current_grants=current_grants,
            current_initiatives_raw=current_initiatives_raw,
        )

        # Step 7b (D3): resolve abstract diff kinds → concrete operation kinds
        await _apply_status_reasoning(self._session, items_data, diff_descriptors, subject_uuid)

        # Step 7c (D4): build descriptor map for DAG resolution
        applications: set[str] = {item.application for item in items_data}
        descriptor_map = await _fetch_descriptor_map(self._session, applications)
        current_account_states = await _fetch_account_states(self._session, applications, subject_uuid)

        # Step 8: content hash + dedup
        content_hash = _compute_content_hash(diff_descriptors)

        if idempotency_key is None:
            cached = await find_recent_active_plan_by_content_hash(
                self._session, subject_ref, content_hash, effective_now
            )
            if cached is not None:
                return cached

        # Step 9: safe-revoke threshold
        remove_count = sum(1 for d in diff_descriptors if d.get('abstract_kind') == _ABSTRACT_REMOVE)
        requires_confirmation = False
        if remove_count > 0:
            total_facts = await count_current_effective_grants(self._session, subject_uuid, effective_now)
            if total_facts > 0:
                threshold = self._settings.safe_revoke_threshold
                if remove_count / total_facts > threshold:
                    requires_confirmation = True

        # Resolve supersedes_plan_id BEFORE creating the new plan
        prev_active = await find_active_plan_for_subject(self._session, subject_ref)
        supersedes_plan_id = prev_active.id if prev_active is not None else None

        # Step 10: persist
        plan_id = uuid.uuid4()
        plan = AccessPlan(
            id=plan_id,
            subject_ref=subject_ref,
            subject_type=subject_kind.value,
            idempotency_key=idempotency_key,
            content_hash=content_hash,
            status=AccessPlanStatus.active,
            requires_confirmation=requires_confirmation,
            supersedes_plan_id=supersedes_plan_id,
        )
        self._session.add(plan)
        await self._session.flush()

        # Fix plan_id in items and persist
        for item in items_data:
            item.plan_id = plan_id
        await insert_plan_items(self._session, items_data)

        # Step 7d (D4): resolve DAG — expand cascades, build deps, detect cycles
        dag_result = _run_dag_resolution(plan_id, items_data, descriptor_map, current_account_states)

        # Persist cascade-synthesised items (injected after D3)
        if dag_result.added_items:
            for synthetic in dag_result.added_items:
                synthetic.plan_id = plan_id
            await insert_plan_items(self._session, dag_result.added_items)

        # Mark plan requires_confirmation if cycle detected
        if dag_result.cycle_detected:
            requires_confirmation = True
            plan.requires_confirmation = True

        # Persist dependency rows
        if dag_result.dependencies:
            await insert_plan_dependencies(self._session, dag_result.dependencies)

        # Auto-supersede older active plans (excluding the new one)
        superseded_count = await supersede_older_active_plans(self._session, subject_ref, plan_id, effective_now)

        await self._session.flush()

        all_items_count = len(items_data) + len(dag_result.added_items)

        # Step 11: emit event
        await self._events.emit(_build_plan_created_event(plan, all_items_count, superseded_count, corr_id))

        return plan

    async def _acquire_subject_lock(self, subject_ref: str) -> None:
        """Acquire a transaction-scoped advisory lock on subject_ref.

        Converts subject_ref to a 64-bit integer via xxhash-style truncation
        of SHA-256. The lock is released automatically when the TX ends.
        """
        lock_key = _subject_ref_to_lock_key(subject_ref)
        await self._session.execute(sa.select(sa.func.pg_advisory_xact_lock(lock_key)))

    async def _build_subject_context(
        self,
        subject_ref: str,
        subject_kind: SubjectKind,
    ) -> SubjectContext:
        """Fetch context from inventory and build SubjectContext.

        subject_ref is a Subject.id UUID.  We first resolve the principal UUID
        (principal_employee_id or principal_nhi_id) from the subjects table, then
        delegate to the appropriate context-data fetcher.
        """
        subject_uuid = UUID(subject_ref)

        employee_principal_id, nhi_principal_id = await fetch_subject_principal_ids(self._session, subject_uuid)

        if subject_kind == SubjectKind.employee:
            if employee_principal_id is None:
                raise SubjectContextNotFoundError(subject_ref, 'employee')
            data = await fetch_employee_context_data(self._session, employee_principal_id)
            if data is None:
                raise SubjectContextNotFoundError(subject_ref, 'employee')
            return SubjectContext(
                subject_ref=subject_ref,
                subject_type='employee',
                org_unit_id=data.get('org_unit_id'),
                attributes=data.get('attributes', {}),
            )
        else:
            # NHI (customer not yet in scope for access planning)
            if nhi_principal_id is None:
                raise SubjectContextNotFoundError(subject_ref, 'nhi')
            data = await fetch_nhi_context_data(self._session, nhi_principal_id)
            if data is None:
                raise SubjectContextNotFoundError(subject_ref, 'nhi')
            return SubjectContext(
                subject_ref=subject_ref,
                subject_type='nhi',
                application_ref=data.get('application_ref'),
                owner_subject_ref=data.get('owner_subject_ref'),
                expires_at=data.get('expires_at'),
                attributes=data.get('attributes', {}),
            )


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions for testability)
# ---------------------------------------------------------------------------


def _subject_ref_to_lock_key(subject_ref: str) -> int:
    """Convert subject_ref to a signed 64-bit int for pg_advisory_xact_lock."""
    digest = hashlib.sha256(subject_ref.encode()).digest()
    # Take first 8 bytes as unsigned, then interpret as signed 64-bit
    unsigned = int.from_bytes(digest[:8], 'big')
    # Wrap to signed 64-bit range
    if unsigned >= (1 << 63):
        return unsigned - (1 << 64)
    return unsigned


def _grants_to_current_facts(grants: list[EffectiveGrant]) -> list[CurrentFact]:
    """Convert effective grants to CurrentFact list for PDP."""
    return [
        CurrentFact(
            application=str(grant.application_id),
            target_descriptor={'resource_id': str(grant.resource_id), 'action': str(grant.action)},
            fact_kind='access',
        )
        for grant in grants
    ]


def _initiatives_to_current_initiatives(initiatives: list[Initiative]) -> list[CurrentInitiative]:
    """Convert Initiative ORM rows to CurrentInitiative list for PDP."""
    result = []
    for init in initiatives:
        result.append(
            CurrentInitiative(
                id=init.id,
                access_fact_id=init.access_fact_id,
                type=init.type,
                origin=init.origin,
                valid_from=init.valid_from,
                valid_until=init.valid_until,
                application='',  # Not directly available from initiative alone
                target_descriptor={},
            )
        )
    return result


def _compute_diff(
    plan_id: UUID,
    desired: list[ProjectedFact],
    current_grants: list[EffectiveGrant],
    current_initiatives_raw: list[Initiative],
) -> tuple[list[PlanItem], list[dict[str, Any]]]:
    """Compute diff between desired and current effective state.

    Returns (plan_items, diff_descriptors) where:
    - plan_items: PlanItem list to persist
    - diff_descriptors: raw dicts used for content_hash computation
    """
    # Build lookup for current effective grants by key
    current_keys: dict[str, EffectiveGrant] = {}
    for grant in current_grants:
        key = _grant_key(grant)
        current_keys[key] = grant

    # Build desired keys
    desired_by_key: dict[str, ProjectedFact] = {}
    for pf in desired:
        key = _projected_fact_key(pf)
        desired_by_key[key] = pf

    items: list[PlanItem] = []
    descriptors: list[dict[str, Any]] = []

    # add_fact: in desired but not in current
    for key, pf in desired_by_key.items():
        if key not in current_keys:
            item, desc = _build_add_fact_item(plan_id, pf)
            items.append(item)
            descriptors.append(desc)

    # remove_fact: in current but not in desired
    # Build initiative lookup by access_fact_id for coverage
    init_by_fact: dict[uuid.UUID, list[Initiative]] = {}
    for init in current_initiatives_raw:
        init_by_fact.setdefault(init.access_fact_id, []).append(init)

    for key, grant in current_keys.items():
        if key not in desired_by_key:
            # Find initiatives covering this grant (via source_initiative_id)
            covering = []
            if hasattr(grant, 'source_initiative_id') and grant.source_initiative_id:
                # Find initiative by id from raw list
                for init in current_initiatives_raw:
                    if init.id == grant.source_initiative_id:
                        covering.append(init)
                        break

            target_descriptor = {
                'resource_id': str(grant.resource_id),
                'action': str(grant.action),
            }
            remove_decision: dict[str, Any] = {
                'abstract_state': 'disabled',
                'actions': [],
                'signals': ['revoke_no_desired_fact'],
                'reasons': [{'rule_id': 'revoke:no_desired_fact', 'rule_kind': 'revoke'}],
            }
            item, desc = _build_remove_fact_item(
                plan_id,
                str(grant.application_id),
                target_descriptor,
                covering,
                remove_decision,
            )
            items.append(item)
            descriptors.append(desc)

    return items, descriptors


# ---------------------------------------------------------------------------
# D4: DAG resolution helpers
# ---------------------------------------------------------------------------


async def _fetch_descriptor_map(
    session: Any,
    applications: set[str],
) -> dict[str, Any]:
    """Fetch ConnectorCapabilityDescriptor for each application in the set."""
    from src.platform.connectors.registration_schemas import ConnectorCapabilityDescriptor

    result: dict[str, Any] = {}
    for app_str in applications:
        descriptor = await fetch_connector_descriptor(session, app_str)
        if descriptor is not None:
            result[app_str] = descriptor
        else:
            result[app_str] = ConnectorCapabilityDescriptor()
    return result


async def _fetch_account_states(
    session: Any,
    applications: set[str],
    subject_uuid: uuid.UUID,
) -> dict[str, str]:
    """Fetch current account status for each application."""
    states: dict[str, str] = {}
    for app_str in applications:
        try:
            app_uuid = uuid.UUID(app_str)
        except ValueError:
            states[app_str] = NOT_EXISTS
            continue
        raw_status = await fetch_account_status_for_subject(session, app_uuid, subject_uuid)
        states[app_str] = raw_status if raw_status is not None else NOT_EXISTS
    return states


def _run_dag_resolution(
    plan_id: uuid.UUID,
    items: list[PlanItem],
    descriptor_map: dict[str, Any],
    current_account_states: dict[str, str],
) -> DAGResult:
    """Run the DAG resolver (pure computation)."""
    return resolve_dag(
        plan_id=plan_id,
        items=items,
        descriptor_map=descriptor_map,
        current_account_states=current_account_states,
    )


# ---------------------------------------------------------------------------
# D3: Account-status reasoning — populate concrete PlanItem.kind
# ---------------------------------------------------------------------------


async def _apply_status_reasoning(
    session: Any,
    items: list[PlanItem],
    descriptors: list[dict[str, Any]],
    subject_uuid: uuid.UUID,
) -> None:
    """Resolve abstract diff kinds to concrete PlanItemKind values (D3).

    Mutates ``items`` in-place — replaces the _PLACEHOLDER_KIND sentinel with
    the actual operation kind determined by:
    - Current account.status for the (application, subject) pair.
    - Connector AccountStatusTransitions from the descriptor.
    - Fact-kind from target_descriptor (role / group / entitlement).

    DB reads are batched per application to avoid N+1:
    - One account status query per unique application.
    - One connector transitions query per unique application (via instance_id
      heuristic: application string used as instance_id fallback, matching the
      mock connector convention from B1 tests).
    """
    # Collect unique applications
    applications: set[str] = {item.application for item in items}

    # Fetch account status for (application, subject) per application
    account_status_map: dict[str, str] = {}
    transitions_map: dict[str, Any] = {}

    for app_str in applications:
        try:
            app_uuid = uuid.UUID(app_str)
        except ValueError:
            app_uuid = None  # type: ignore[assignment]

        if app_uuid is not None:
            raw_status = await fetch_account_status_for_subject(session, app_uuid, subject_uuid)
        else:
            raw_status = None

        account_status_map[app_str] = raw_status if raw_status is not None else NOT_EXISTS
        # Use app_str as connector instance_id (convention from mock_connector / B1)
        transitions_map[app_str] = await fetch_connector_transitions(session, app_str)

    for item, desc in zip(items, descriptors, strict=False):
        abstract_kind = desc.get('abstract_kind', _ABSTRACT_ADD)
        is_account_item = item.target_descriptor.get('fact_kind') == 'account' or desc.get('is_account_item', False)
        current_status = account_status_map.get(item.application, NOT_EXISTS)
        transitions = transitions_map.get(item.application)

        item.kind = resolve_item_kind(
            abstract_kind=abstract_kind,
            is_account_item=is_account_item,
            current_account_status=current_status,
            transitions=transitions,
            target_descriptor=item.target_descriptor,
        )
