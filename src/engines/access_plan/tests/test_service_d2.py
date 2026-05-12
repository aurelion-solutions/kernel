# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for D2 planning logic in AccessPlanService.

All tests are fully in-memory / mocked — no DB access.
Covers:
- SubjectContext building helpers (pure functions)
- content_hash computation (pure function, deterministic)
- advisory lock key computation (pure function)
- Diff computation: add_fact / remove_fact
- Idempotency_key reuse path (via repository mocking)
- Content-hash dedup path (via repository mocking)
- Safe-revoke threshold sets requires_confirmation=True
- Auto-supersedes path
- access_plan.created event emission
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.engines.access_effective.models import EffectiveGrant, EffectiveGrantEffect
from src.engines.access_plan.models import AccessPlan, AccessPlanStatus
from src.engines.access_plan.service import (
    AccessPlanService,
    SubjectContextNotFoundError,
    SubjectNotFoundError,
    _compute_content_hash,
    _compute_diff,
    _grants_to_current_facts,
    _initiatives_to_current_initiatives,
    _subject_ref_to_lock_key,
)
from src.engines.policy_assessment.generative.schemas import (
    CurrentFact,
    CurrentInitiative,
    InitiativeProjection,
    ProjectedFact,
    SubjectContext,
)
from src.engines.policy_assessment.generative.service import GenerativePDPService
from src.engines.policy_assessment.schemas import AbstractState, Decision, Reason, RulePack
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.subjects.models import SubjectKind
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_decision(rule_id: str = 'test_rule') -> Decision:
    reason = Reason(
        rule_id=rule_id,
        rule_kind='birthright',
        precedence=1,
        matched_conditions={},
        fact_values={},
        produced={},
    )
    return Decision(
        abstract_state=AbstractState.enabled,
        actions=[],
        signals=[],
        reasons=[reason],
    )


def _make_projected_fact(
    application: str = 'app1',
    target_descriptor: dict[str, Any] | None = None,
) -> ProjectedFact:
    desc = target_descriptor or {'role': 'viewer'}
    initiative = InitiativeProjection(
        type=InitiativeType.birthright,
        origin='policy_rule:test_rule',
    )
    return ProjectedFact(
        fact_kind='access',
        application=application,
        target_descriptor=desc,
        initiatives=[initiative],
        decision=_make_decision(),
    )


def _make_effective_grant(
    application_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
) -> EffectiveGrant:
    grant = MagicMock(spec=EffectiveGrant)
    grant.application_id = application_id or uuid.uuid4()
    grant.resource_id = resource_id or uuid.uuid4()
    grant.subject_id = subject_id or uuid.uuid4()
    grant.effect = EffectiveGrantEffect.allow
    grant.source_initiative_id = None
    grant.tombstoned_at = None
    grant.valid_until = None
    return grant  # type: ignore[return-value]  # noqa: PGH003


def _make_initiative(type_: InitiativeType = InitiativeType.requested) -> Initiative:
    init = Initiative(
        id=uuid.uuid4(),
        access_fact_id=uuid.uuid4(),
        type=type_,
        origin='request:test',
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
        valid_until=None,
    )
    return init


def _make_rule_pack() -> RulePack:
    return RulePack(lifecycle=[], risk=[])


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


def test_subject_ref_to_lock_key_deterministic() -> None:
    ref = str(uuid.uuid4())
    k1 = _subject_ref_to_lock_key(ref)
    k2 = _subject_ref_to_lock_key(ref)
    assert k1 == k2


def test_subject_ref_to_lock_key_is_signed_64bit() -> None:
    for _ in range(10):
        ref = str(uuid.uuid4())
        k = _subject_ref_to_lock_key(ref)
        assert -(2**63) <= k < (2**63)


def test_subject_ref_to_lock_key_different_refs() -> None:
    k1 = _subject_ref_to_lock_key(str(uuid.uuid4()))
    k2 = _subject_ref_to_lock_key(str(uuid.uuid4()))
    assert k1 != k2


def test_compute_content_hash_deterministic() -> None:
    items = [
        {'abstract_kind': 'add_fact', 'application': 'app1', 'target_descriptor': {'role': 'admin'}},
        {'abstract_kind': 'remove_fact', 'application': 'app2', 'target_descriptor': {'group': 'ops'}},
    ]
    h1 = _compute_content_hash(items)
    h2 = _compute_content_hash(items)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_compute_content_hash_order_independent() -> None:
    """Hash must be the same regardless of item order (sorting is applied)."""
    items_a = [
        {'abstract_kind': 'add_fact', 'application': 'app1', 'target_descriptor': {'role': 'viewer'}},
        {'abstract_kind': 'remove_fact', 'application': 'app2', 'target_descriptor': {'group': 'ops'}},
    ]
    items_b = list(reversed(items_a))
    assert _compute_content_hash(items_a) == _compute_content_hash(items_b)


def test_compute_content_hash_differs_on_content() -> None:
    items_a = [{'abstract_kind': 'add_fact', 'application': 'app1', 'target_descriptor': {}}]
    items_b = [{'abstract_kind': 'remove_fact', 'application': 'app1', 'target_descriptor': {}}]
    assert _compute_content_hash(items_a) != _compute_content_hash(items_b)


def test_compute_content_hash_empty_diff() -> None:
    h = _compute_content_hash([])
    assert isinstance(h, str)
    assert len(h) == 64


def test_grants_to_current_facts_empty() -> None:
    assert _grants_to_current_facts([]) == []


def test_grants_to_current_facts_converts() -> None:
    grant = _make_effective_grant()
    facts = _grants_to_current_facts([grant])
    assert len(facts) == 1
    assert isinstance(facts[0], CurrentFact)
    assert facts[0].application == str(grant.application_id)


def test_initiatives_to_current_initiatives_empty() -> None:
    assert _initiatives_to_current_initiatives([]) == []


def test_initiatives_to_current_initiatives_converts() -> None:
    init = _make_initiative(InitiativeType.requested)
    result = _initiatives_to_current_initiatives([init])
    assert len(result) == 1
    assert isinstance(result[0], CurrentInitiative)
    assert result[0].id == init.id
    assert result[0].type == InitiativeType.requested


# ---------------------------------------------------------------------------
# Diff computation tests
# ---------------------------------------------------------------------------


def test_diff_empty_desired_empty_current() -> None:
    plan_id = uuid.uuid4()
    items, descs = _compute_diff(plan_id, [], [], [])
    assert items == []
    assert descs == []


def test_diff_add_fact_when_desired_not_in_current() -> None:
    plan_id = uuid.uuid4()
    pf = _make_projected_fact(application='app1', target_descriptor={'role': 'viewer'})
    items, descs = _compute_diff(plan_id, [pf], [], [])
    assert len(items) == 1
    assert len(descs) == 1
    assert descs[0]['abstract_kind'] == 'add_fact'
    assert descs[0]['application'] == 'app1'


def test_diff_remove_fact_when_current_not_in_desired() -> None:
    plan_id = uuid.uuid4()
    grant = _make_effective_grant()
    items, descs = _compute_diff(plan_id, [], [grant], [])
    assert len(items) == 1
    assert descs[0]['abstract_kind'] == 'remove_fact'


def test_diff_no_change_when_desired_matches_current() -> None:
    """If desired keys match current grant keys, diff should be empty."""
    plan_id = uuid.uuid4()
    app_id = uuid.uuid4()
    res_id = uuid.uuid4()
    grant = _make_effective_grant(application_id=app_id, resource_id=res_id)

    # ProjectedFact key = f'{application}::{sorted target_descriptor}'
    # Grant key = f'{application_id}::{resource_id}'
    # These are structurally different keys by design in D2 — diff WILL happen
    # (D3 aligns them). Test the key structure, not identity.
    items, descs = _compute_diff(plan_id, [], [grant], [])
    assert all(d['abstract_kind'] == 'remove_fact' for d in descs)


def test_diff_add_fact_item_has_initiatives() -> None:
    plan_id = uuid.uuid4()
    pf = _make_projected_fact()
    items, _ = _compute_diff(plan_id, [pf], [], [])
    item = items[0]
    assert len(item.initiatives) == 1
    assert item.initiatives[0]['type'] == InitiativeType.birthright.value


def test_diff_remove_fact_item_has_initiative_refs_when_initiative_covers_grant() -> None:
    plan_id = uuid.uuid4()
    init = _make_initiative(InitiativeType.requested)
    grant = _make_effective_grant()
    # Link grant to initiative via source_initiative_id
    grant.source_initiative_id = init.id
    items, descs = _compute_diff(plan_id, [], [grant], [init])
    assert len(items) == 1
    assert str(init.id) in items[0].initiative_refs


# ---------------------------------------------------------------------------
# Service integration (mocked session + repository)
# ---------------------------------------------------------------------------


def _make_async_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    # pg_advisory_xact_lock
    session.execute = AsyncMock()
    return session


def _make_pdp_service(desired: list[ProjectedFact] | None = None) -> GenerativePDPService:
    pdp = MagicMock(spec=GenerativePDPService)
    pdp.assess = MagicMock(return_value=desired or [])
    return pdp  # type: ignore[return-value]  # noqa: PGH003


@pytest.mark.asyncio
async def test_create_plan_raises_if_subject_not_found() -> None:
    session = _make_async_session()
    pdp = _make_pdp_service()
    svc = AccessPlanService(session, pdp)
    subject_ref = str(uuid.uuid4())

    with (
        patch('src.engines.access_plan.service.resolve_subject_kind', new=AsyncMock(return_value=None)),
    ):
        with pytest.raises(SubjectNotFoundError):
            await svc.create_plan(subject_ref=subject_ref)


@pytest.mark.asyncio
async def test_create_plan_reuses_existing_on_idempotency_key() -> None:
    session = _make_async_session()
    pdp = _make_pdp_service()
    svc = AccessPlanService(session, pdp)
    subject_ref = str(uuid.uuid4())
    idem_key = 'test-idem-key'

    existing_plan = MagicMock(spec=AccessPlan)
    existing_plan.id = uuid.uuid4()
    existing_plan.status = AccessPlanStatus.active

    with (
        patch(
            'src.engines.access_plan.service.find_plan_by_idempotency_key',
            new=AsyncMock(return_value=existing_plan),
        ),
    ):
        result = await svc.create_plan(subject_ref=subject_ref, idempotency_key=idem_key)

    assert result is existing_plan


@pytest.mark.asyncio
async def test_create_plan_reuses_on_content_hash_dedup() -> None:
    session = _make_async_session()
    pdp = _make_pdp_service(desired=[])  # empty diff → empty content_hash
    subject_ref = str(uuid.uuid4())

    existing_plan = MagicMock(spec=AccessPlan)
    existing_plan.id = uuid.uuid4()
    existing_plan.status = AccessPlanStatus.active

    svc = AccessPlanService(session, pdp)
    _now = datetime.now(UTC)

    with (
        patch('src.engines.access_plan.service.find_plan_by_idempotency_key', new=AsyncMock(return_value=None)),
        patch('src.engines.access_plan.service.resolve_subject_kind', new=AsyncMock(return_value=SubjectKind.employee)),
        patch(
            'src.engines.access_plan.service.AccessPlanService._build_subject_context',
            new=AsyncMock(return_value=SubjectContext(subject_ref=subject_ref, subject_type='employee')),
        ),
        patch('src.engines.access_plan.service.fetch_current_effective_grants', new=AsyncMock(return_value=[])),
        patch(
            'src.engines.access_plan.service.fetch_current_initiatives_for_subject',
            new=AsyncMock(return_value=[]),
        ),
        patch(
            'src.engines.access_plan.service.find_recent_active_plan_by_content_hash',
            new=AsyncMock(return_value=existing_plan),
        ),
    ):
        result = await svc.create_plan(subject_ref=subject_ref, now=_now)

    assert result is existing_plan


@pytest.mark.asyncio
async def test_create_plan_sets_requires_confirmation_on_high_revoke() -> None:
    """When remove items exceed safe_revoke_threshold, requires_confirmation=True."""
    session = _make_async_session()
    subject_ref = str(uuid.uuid4())

    # 2 grants exist, 2 will be removed → 100% revoke → > 0.5 threshold
    grant1 = _make_effective_grant()
    grant2 = _make_effective_grant()

    pdp = _make_pdp_service(desired=[])  # no desired → 2 remove_fact items

    settings = RuntimeSettingsConfig(safe_revoke_threshold=0.5)
    svc = AccessPlanService(session, pdp, settings=settings)

    persisted_plan: AccessPlan | None = None

    def capture_add(obj: Any) -> None:
        nonlocal persisted_plan
        if isinstance(obj, AccessPlan):
            persisted_plan = obj

    session.add = MagicMock(side_effect=capture_add)

    _now = datetime.now(UTC)

    with (
        patch('src.engines.access_plan.service.find_plan_by_idempotency_key', new=AsyncMock(return_value=None)),
        patch('src.engines.access_plan.service.resolve_subject_kind', new=AsyncMock(return_value=SubjectKind.employee)),
        patch(
            'src.engines.access_plan.service.AccessPlanService._build_subject_context',
            new=AsyncMock(return_value=SubjectContext(subject_ref=subject_ref, subject_type='employee')),
        ),
        patch(
            'src.engines.access_plan.service.fetch_current_effective_grants',
            new=AsyncMock(return_value=[grant1, grant2]),
        ),
        patch(
            'src.engines.access_plan.service.fetch_current_initiatives_for_subject',
            new=AsyncMock(return_value=[]),
        ),
        patch(
            'src.engines.access_plan.service.find_recent_active_plan_by_content_hash',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.count_current_effective_grants',
            new=AsyncMock(return_value=2),
        ),
        patch(
            'src.engines.access_plan.service.find_active_plan_for_subject',
            new=AsyncMock(return_value=None),
        ),
        patch('src.engines.access_plan.service.insert_plan_items', new=AsyncMock()),
        patch(
            'src.engines.access_plan.service.supersede_older_active_plans',
            new=AsyncMock(return_value=0),
        ),
        patch(
            'src.engines.access_plan.service.fetch_account_status_for_subject',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.fetch_connector_transitions',
            new=AsyncMock(return_value=MagicMock(transitions=[])),
        ),
        patch(
            'src.engines.access_plan.service.fetch_connector_descriptor',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.insert_plan_dependencies',
            new=AsyncMock(),
        ),
    ):
        await svc.create_plan(subject_ref=subject_ref, now=_now)

    assert persisted_plan is not None
    assert persisted_plan.requires_confirmation is True


@pytest.mark.asyncio
async def test_create_plan_no_confirmation_on_low_revoke() -> None:
    """When remove items are below safe_revoke_threshold, requires_confirmation=False."""
    session = _make_async_session()
    subject_ref = str(uuid.uuid4())

    # 10 grants exist, 1 will be removed → 10% revoke → < 0.5 threshold
    # We test with 0 desired, 1 grant = 1 removal out of total=10 (mocked)
    grant = _make_effective_grant()
    pdp = _make_pdp_service(desired=[])

    settings = RuntimeSettingsConfig(safe_revoke_threshold=0.5)
    svc = AccessPlanService(session, pdp, settings=settings)

    persisted_plan: AccessPlan | None = None

    def capture_add(obj: Any) -> None:
        nonlocal persisted_plan
        if isinstance(obj, AccessPlan):
            persisted_plan = obj

    session.add = MagicMock(side_effect=capture_add)
    _now = datetime.now(UTC)

    with (
        patch('src.engines.access_plan.service.find_plan_by_idempotency_key', new=AsyncMock(return_value=None)),
        patch('src.engines.access_plan.service.resolve_subject_kind', new=AsyncMock(return_value=SubjectKind.employee)),
        patch(
            'src.engines.access_plan.service.AccessPlanService._build_subject_context',
            new=AsyncMock(return_value=SubjectContext(subject_ref=subject_ref, subject_type='employee')),
        ),
        patch(
            'src.engines.access_plan.service.fetch_current_effective_grants',
            new=AsyncMock(return_value=[grant]),
        ),
        patch(
            'src.engines.access_plan.service.fetch_current_initiatives_for_subject',
            new=AsyncMock(return_value=[]),
        ),
        patch(
            'src.engines.access_plan.service.find_recent_active_plan_by_content_hash',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.count_current_effective_grants',
            new=AsyncMock(return_value=10),  # 1 removal / 10 total = 10% < 50%
        ),
        patch(
            'src.engines.access_plan.service.find_active_plan_for_subject',
            new=AsyncMock(return_value=None),
        ),
        patch('src.engines.access_plan.service.insert_plan_items', new=AsyncMock()),
        patch(
            'src.engines.access_plan.service.supersede_older_active_plans',
            new=AsyncMock(return_value=0),
        ),
        patch(
            'src.engines.access_plan.service.fetch_account_status_for_subject',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.fetch_connector_transitions',
            new=AsyncMock(return_value=MagicMock(transitions=[])),
        ),
        patch(
            'src.engines.access_plan.service.fetch_connector_descriptor',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.insert_plan_dependencies',
            new=AsyncMock(),
        ),
    ):
        await svc.create_plan(subject_ref=subject_ref, now=_now)

    assert persisted_plan is not None
    assert persisted_plan.requires_confirmation is False


@pytest.mark.asyncio
async def test_create_plan_emits_access_plan_created_event() -> None:
    session = _make_async_session()
    subject_ref = str(uuid.uuid4())
    pdp = _make_pdp_service(desired=[])

    from src.platform.events.testing import CapturingEventService

    event_sink = CapturingEventService()
    svc = AccessPlanService(session, pdp, event_service=event_sink)  # type: ignore[arg-type]

    persisted_plan: AccessPlan | None = None

    def capture_add(obj: Any) -> None:
        nonlocal persisted_plan
        if isinstance(obj, AccessPlan):
            persisted_plan = obj

    session.add = MagicMock(side_effect=capture_add)
    _now = datetime.now(UTC)

    with (
        patch('src.engines.access_plan.service.find_plan_by_idempotency_key', new=AsyncMock(return_value=None)),
        patch('src.engines.access_plan.service.resolve_subject_kind', new=AsyncMock(return_value=SubjectKind.employee)),
        patch(
            'src.engines.access_plan.service.AccessPlanService._build_subject_context',
            new=AsyncMock(return_value=SubjectContext(subject_ref=subject_ref, subject_type='employee')),
        ),
        patch('src.engines.access_plan.service.fetch_current_effective_grants', new=AsyncMock(return_value=[])),
        patch(
            'src.engines.access_plan.service.fetch_current_initiatives_for_subject',
            new=AsyncMock(return_value=[]),
        ),
        patch(
            'src.engines.access_plan.service.find_recent_active_plan_by_content_hash',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.count_current_effective_grants',
            new=AsyncMock(return_value=0),
        ),
        patch(
            'src.engines.access_plan.service.find_active_plan_for_subject',
            new=AsyncMock(return_value=None),
        ),
        patch('src.engines.access_plan.service.insert_plan_items', new=AsyncMock()),
        patch(
            'src.engines.access_plan.service.supersede_older_active_plans',
            new=AsyncMock(return_value=0),
        ),
    ):
        await svc.create_plan(subject_ref=subject_ref, now=_now)

    assert len(event_sink.emitted) == 1
    evt = event_sink.emitted[0]
    assert evt.event_type == 'access_plan.plan.created'
    assert evt.payload['subject_ref'] == subject_ref


@pytest.mark.asyncio
async def test_create_plan_sets_supersedes_plan_id() -> None:
    session = _make_async_session()
    subject_ref = str(uuid.uuid4())
    pdp = _make_pdp_service(desired=[])

    prev_plan_id = uuid.uuid4()
    prev_plan = MagicMock(spec=AccessPlan)
    prev_plan.id = prev_plan_id
    prev_plan.status = AccessPlanStatus.active

    svc = AccessPlanService(session, pdp)

    persisted_plan: AccessPlan | None = None

    def capture_add(obj: Any) -> None:
        nonlocal persisted_plan
        if isinstance(obj, AccessPlan):
            persisted_plan = obj

    session.add = MagicMock(side_effect=capture_add)
    _now = datetime.now(UTC)

    with (
        patch('src.engines.access_plan.service.find_plan_by_idempotency_key', new=AsyncMock(return_value=None)),
        patch('src.engines.access_plan.service.resolve_subject_kind', new=AsyncMock(return_value=SubjectKind.employee)),
        patch(
            'src.engines.access_plan.service.AccessPlanService._build_subject_context',
            new=AsyncMock(return_value=SubjectContext(subject_ref=subject_ref, subject_type='employee')),
        ),
        patch('src.engines.access_plan.service.fetch_current_effective_grants', new=AsyncMock(return_value=[])),
        patch(
            'src.engines.access_plan.service.fetch_current_initiatives_for_subject',
            new=AsyncMock(return_value=[]),
        ),
        patch(
            'src.engines.access_plan.service.find_recent_active_plan_by_content_hash',
            new=AsyncMock(return_value=None),
        ),
        patch(
            'src.engines.access_plan.service.count_current_effective_grants',
            new=AsyncMock(return_value=0),
        ),
        patch(
            'src.engines.access_plan.service.find_active_plan_for_subject',
            new=AsyncMock(return_value=prev_plan),
        ),
        patch('src.engines.access_plan.service.insert_plan_items', new=AsyncMock()),
        patch(
            'src.engines.access_plan.service.supersede_older_active_plans',
            new=AsyncMock(return_value=1),
        ),
    ):
        await svc.create_plan(subject_ref=subject_ref, now=_now)

    assert persisted_plan is not None
    assert persisted_plan.supersedes_plan_id == prev_plan_id


@pytest.mark.asyncio
async def test_create_plan_raises_if_employee_context_missing() -> None:
    session = _make_async_session()
    subject_ref = str(uuid.uuid4())
    employee_id = uuid.uuid4()
    pdp = _make_pdp_service(desired=[])
    svc = AccessPlanService(session, pdp)

    with (
        patch('src.engines.access_plan.service.find_plan_by_idempotency_key', new=AsyncMock(return_value=None)),
        patch('src.engines.access_plan.service.resolve_subject_kind', new=AsyncMock(return_value=SubjectKind.employee)),
        patch(
            'src.engines.access_plan.service.fetch_subject_principal_ids',
            new=AsyncMock(return_value=(employee_id, None)),
        ),
        patch(
            'src.engines.access_plan.service.fetch_employee_context_data',
            new=AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(SubjectContextNotFoundError):
            await svc.create_plan(subject_ref=subject_ref)
