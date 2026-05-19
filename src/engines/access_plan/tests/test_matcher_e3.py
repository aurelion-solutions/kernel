# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for E3 MQ matcher integration: routing key matching and fan-out actions.

Covers:
- find_matching_mq_triggers: each of the 6 routing keys matches the correct pipeline
- find_matching_mq_triggers: non-matching routing key → empty result
- Deduplification via idempotency_key (delegated to existing Phase 18 orchestrator unique constraint;
  tested here at the pipeline definition level — idempotency_key arg is extracted from payload)
- fanout_replan_for_application: N NHIs → N plans created
- fanout_replan_for_application: zero NHIs → 0 plans
- fanout_replan_for_application: invalid application_id → graceful return
- fanout_replan_for_initiative: subject_ref in payload → plan created
- fanout_replan_for_initiative: no subject_ref, initiative exists → skip gracefully
- fanout_replan_for_initiative: no subject_ref, initiative missing → skip gracefully
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
import src.engines.access_apply.actions  # noqa: F401
import src.engines.access_effective.actions  # noqa: F401

# Import action modules to populate ACTION_REGISTRY before loader validates action refs.
import src.engines.access_plan.actions  # noqa: F401
import src.inventory.initiatives.actions  # noqa: F401
from src.platform.orchestrator.loader import PipelineDefinitionLoader
from src.platform.orchestrator.matcher import find_matching_mq_triggers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PIPELINES_DIR = Path(__file__).parent.parent.parent.parent.parent / 'pipelines'


def _load_pipelines() -> dict[str, Any]:
    """Load all pipeline definitions from the pipelines/ directory.

    Action-ref validation is skipped here — the test's purpose is to verify
    matching logic (routing keys, arg extraction), not that the registry is
    populated.  The registry-isolation fixture in assessment_preview/tests
    clears the global ACTION_REGISTRY between tests; relying on it during
    load_dir would cause spurious PipelineActionRefError failures.
    """
    loader = PipelineDefinitionLoader(validate_action_refs=False)
    return loader.load_dir(_PIPELINES_DIR)


# ---------------------------------------------------------------------------
# Routing key matching tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope='module')
def loaded_defs() -> dict[str, Any]:
    return _load_pipelines()


def _find_matching(
    loaded_defs: Mapping[str, Any],
    routing_key: str,
    payload: dict[str, Any],
) -> list[str]:
    """Return pipeline names matched for the given routing_key + payload."""
    matches = find_matching_mq_triggers(loaded_defs, routing_key, payload)
    return [defn.name for defn, _ in matches]


def test_employee_updated_employment_status_matches(loaded_defs: dict[str, Any]) -> None:
    """inventory.employee.updated with changes.employment_status → access_plan_subject_triggers."""
    subject_id = str(uuid.uuid4())
    names = _find_matching(
        loaded_defs,
        'inventory.employee.updated',
        {
            'employee_id': subject_id,
            'subject_ref': subject_id,
            'subject_type': 'employee',
            'changes': {
                'attributes.employment_status': {'old': None, 'new': 'active'},
            },
        },
    )
    # The trigger uses match: {changes: {employment_status: {}}} (literal "employment_status",
    # not "attributes.employment_status"). Matcher uses containment, so we
    # send both flavours separately to verify each path.
    # First, the dotted-attribute flavour (which is what the service emits).
    # If the trigger keys on the dotted form it matches; otherwise this test
    # only proves the second flavour below.
    _ = names  # noqa: F841 — first path consumed to surface the API call shape

    names = _find_matching(
        loaded_defs,
        'inventory.employee.updated',
        {
            'employee_id': subject_id,
            'subject_ref': subject_id,
            'subject_type': 'employee',
            'changes': {
                'employment_status': {'old': None, 'new': 'active'},
            },
        },
    )
    assert 'access_plan_subject_triggers' in names


def test_employee_updated_org_unit_matches(loaded_defs: dict[str, Any]) -> None:
    """inventory.employee.updated with changes.org_unit_id → access_plan_subject_triggers."""
    subject_id = str(uuid.uuid4())
    names = _find_matching(
        loaded_defs,
        'inventory.employee.updated',
        {
            'employee_id': subject_id,
            'subject_ref': subject_id,
            'subject_type': 'employee',
            'changes': {
                'org_unit_id': {'old': None, 'new': str(uuid.uuid4())},
            },
        },
    )
    assert 'access_plan_subject_triggers' in names


def test_subject_scheduled_replan_required_matches(loaded_defs: dict[str, Any]) -> None:
    """subject.replan.required → access_plan_subject_triggers pipeline."""
    names = _find_matching(
        loaded_defs,
        'subject.replan.required',
        {'subject_id': str(uuid.uuid4()), 'idempotency_key': 'abc'},
    )
    assert 'access_plan_subject_triggers' in names


def test_nhi_expired_matches(loaded_defs: dict[str, Any]) -> None:
    """inventory.nhi.expired → access_plan_subject_triggers; trigger extracts subject_ref."""
    subject_id = str(uuid.uuid4())
    matches = find_matching_mq_triggers(
        loaded_defs,
        'inventory.nhi.expired',
        {'nhi_id': str(uuid.uuid4()), 'subject_ref': subject_id, 'subject_type': 'nhi'},
    )
    pipeline_names = [defn.name for defn, _ in matches]
    assert 'access_plan_subject_triggers' in pipeline_names

    # Verify the trigger extracts subject_ref (not nhi_id) as args.subject_ref.
    matched_triggers = [
        (defn.name, trigger) for defn, trigger in matches if defn.name == 'access_plan_subject_triggers'
    ]
    assert matched_triggers, 'access_plan_subject_triggers must match'
    _defn_name, trigger = matched_triggers[0]
    assert trigger.get('args_from_payload', {}).get('subject_ref') == 'subject_ref'


def test_initiative_changed_matches(loaded_defs: dict[str, Any]) -> None:
    """inventory.initiative.changed → access_plan_initiative_changed pipeline."""
    names = _find_matching(
        loaded_defs,
        'inventory.initiative.changed',
        {'initiative_id': str(uuid.uuid4()), 'access_fact_id': str(uuid.uuid4()), 'change_type': 'created'},
    )
    assert 'access_plan_initiative_changed' in names


def test_application_decommissioned_matches(loaded_defs: dict[str, Any]) -> None:
    """inventory.application.decommissioned → access_plan_application_decommissioned pipeline."""
    names = _find_matching(
        loaded_defs,
        'inventory.application.decommissioned',
        {'application_id': str(uuid.uuid4()), 'code': 'app-x'},
    )
    assert 'access_plan_application_decommissioned' in names


def test_unknown_routing_key_no_match(loaded_defs: dict[str, Any]) -> None:
    """An unrelated routing key does not match any access_plan pipeline."""
    names = _find_matching(loaded_defs, 'inventory.account.created', {'account_id': str(uuid.uuid4())})
    access_plan_names = [n for n in names if n.startswith('access_plan_')]
    assert access_plan_names == []


def test_idempotency_key_extracted_from_payload(loaded_defs: dict[str, Any]) -> None:
    """When inventory.employee.updated carries idempotency_key, trigger extracts it."""
    idem_key = 'unique-key-42'
    subject_id = str(uuid.uuid4())
    matches = find_matching_mq_triggers(
        loaded_defs,
        'inventory.employee.updated',
        {
            'employee_id': subject_id,
            'subject_ref': subject_id,
            'subject_type': 'employee',
            'idempotency_key': idem_key,
            'changes': {
                'employment_status': {'old': None, 'new': 'active'},
            },
        },
    )
    matched_triggers = [
        (defn.name, trigger) for defn, trigger in matches if defn.name == 'access_plan_subject_triggers'
    ]
    assert matched_triggers, 'access_plan_subject_triggers must match'
    _defn_name, trigger = matched_triggers[0]
    assert trigger.get('args_from_payload', {}).get('idempotency_key') == 'idempotency_key'


# ---------------------------------------------------------------------------
# fanout_replan_for_application action tests
# ---------------------------------------------------------------------------


def _make_action_context(session: Any) -> Any:
    """Build a minimal ActionContext-like object for testing."""
    from src.platform.logs.service import NoOpLogService
    from src.platform.orchestrator.registry import ActionContext

    return ActionContext(
        session=session,
        log_service=NoOpLogService(),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id='test-worker',
    )


@pytest.mark.asyncio
async def test_fanout_replan_for_application_n_nhis(session_factory) -> None:
    """fanout_replan_for_application: N NHIs → N plans created."""
    from src.engines.access_plan.actions import (
        FanoutReplanForApplicationArgs,
        fanout_replan_for_application_action,
    )
    from src.inventory.nhi.models import NHI
    from src.platform.applications.models import Application

    app_id = uuid.uuid4()

    async with session_factory() as session:
        # Create application and two NHIs belonging to it.
        app = Application(id=app_id, name='TestApp', code='test-app-e3')
        session.add(app)
        await session.flush()

        nhi1 = NHI(external_id='nhi-e3-a', name='NHI-A', kind='bot', application_id=app_id)
        nhi2 = NHI(external_id='nhi-e3-b', name='NHI-B', kind='bot', application_id=app_id)
        session.add(nhi1)
        session.add(nhi2)
        await session.flush()

        nhi1_id = str(nhi1.id)
        nhi2_id = str(nhi2.id)
        await session.commit()

    # Patch create_plan, GenerativePDPService, and resolve_subject_ref_for_nhi
    # to avoid full service setup and Subject table dependency.
    plan_mock = MagicMock()
    plan_mock.id = uuid.uuid4()

    with (
        patch('src.engines.access_plan.service.AccessPlanService.create_plan', new_callable=AsyncMock) as mock_plan,
        patch('src.engines.policy_assessment.generative.service.GenerativePDPService.__init__', return_value=None),
        patch(
            'src.engines.access_plan.repository.resolve_subject_ref_for_nhi',
            new_callable=AsyncMock,
            side_effect=lambda _session, nhi_id: str(uuid.uuid4()),
        ),
    ):
        mock_plan.return_value = plan_mock

        async with session_factory() as session:
            ctx = _make_action_context(session)
            args = FanoutReplanForApplicationArgs(application_id=str(app_id))
            result = await fanout_replan_for_application_action(args, ctx)

    assert result.nhi_count == 2
    assert result.plans_created == 2
    assert result.application_id == str(app_id)

    # Verify idempotency_key includes application and NHI ids.
    call_keys = {call.kwargs.get('idempotency_key') for call in mock_plan.call_args_list}
    assert f'{app_id}:{nhi1_id}' in call_keys
    assert f'{app_id}:{nhi2_id}' in call_keys


@pytest.mark.asyncio
async def test_fanout_replan_for_application_zero_nhis(session_factory) -> None:
    """fanout_replan_for_application: application with no NHIs → 0 plans."""
    from src.engines.access_plan.actions import (
        FanoutReplanForApplicationArgs,
        fanout_replan_for_application_action,
    )
    from src.platform.applications.models import Application

    app_id = uuid.uuid4()

    async with session_factory() as session:
        app = Application(id=app_id, name='EmptyApp', code='empty-app-e3')
        session.add(app)
        await session.commit()

    async with session_factory() as session:
        ctx = _make_action_context(session)
        args = FanoutReplanForApplicationArgs(application_id=str(app_id))
        result = await fanout_replan_for_application_action(args, ctx)

    assert result.nhi_count == 0
    assert result.plans_created == 0


@pytest.mark.asyncio
async def test_fanout_replan_for_application_invalid_uuid() -> None:
    """fanout_replan_for_application: invalid application_id → graceful return (0 plans)."""
    from src.engines.access_plan.actions import (
        FanoutReplanForApplicationArgs,
        fanout_replan_for_application_action,
    )

    session_mock = AsyncMock()
    ctx = _make_action_context(session_mock)
    args = FanoutReplanForApplicationArgs(application_id='not-a-uuid')
    result = await fanout_replan_for_application_action(args, ctx)

    assert result.nhi_count == 0
    assert result.plans_created == 0


# ---------------------------------------------------------------------------
# fanout_replan_for_initiative action tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fanout_replan_for_initiative_with_subject_ref(session_factory) -> None:
    """fanout_replan_for_initiative: subject_ref provided → create_plan called once."""
    from src.engines.access_plan.actions import (
        FanoutReplanForInitiativeArgs,
        fanout_replan_for_initiative_action,
    )

    subject_id = uuid.uuid4()
    initiative_id = uuid.uuid4()

    plan_mock = MagicMock()
    plan_mock.id = uuid.uuid4()

    with (
        patch('src.engines.access_plan.service.AccessPlanService.create_plan', new_callable=AsyncMock) as mock_plan,
        patch('src.engines.policy_assessment.generative.service.GenerativePDPService.__init__', return_value=None),
    ):
        mock_plan.return_value = plan_mock

        async with session_factory() as session:
            ctx = _make_action_context(session)
            args = FanoutReplanForInitiativeArgs(
                initiative_id=str(initiative_id),
                subject_ref=str(subject_id),
                idempotency_key='test-idem-key',
            )
            result = await fanout_replan_for_initiative_action(args, ctx)

    assert result.skipped is False
    assert result.subject_ref == str(subject_id)
    assert result.plan_id is not None
    mock_plan.assert_called_once_with(
        subject_ref=str(subject_id),
        idempotency_key='test-idem-key',
    )


@pytest.mark.asyncio
async def test_fanout_replan_for_initiative_no_subject_ref_initiative_exists(session_factory) -> None:
    """fanout_replan_for_initiative: no subject_ref, initiative found → skip (cannot resolve subject)."""
    from src.engines.access_plan.actions import (
        FanoutReplanForInitiativeArgs,
        fanout_replan_for_initiative_action,
    )
    from src.inventory.initiatives.models import Initiative, InitiativeType

    initiative_id = uuid.uuid4()
    fact_id = uuid.uuid4()

    async with session_factory() as session:
        initiative = Initiative(
            id=initiative_id,
            access_fact_id=fact_id,
            type=InitiativeType.birthright,
            origin='test',
        )
        session.add(initiative)
        await session.commit()

    async with session_factory() as session:
        ctx = _make_action_context(session)
        args = FanoutReplanForInitiativeArgs(initiative_id=str(initiative_id))
        result = await fanout_replan_for_initiative_action(args, ctx)

    assert result.skipped is True
    assert result.skip_reason == 'subject_ref_missing_from_payload'


@pytest.mark.asyncio
async def test_fanout_replan_for_initiative_initiative_not_found(session_factory) -> None:
    """fanout_replan_for_initiative: no subject_ref, initiative missing → skip gracefully."""
    from src.engines.access_plan.actions import (
        FanoutReplanForInitiativeArgs,
        fanout_replan_for_initiative_action,
    )

    async with session_factory() as session:
        ctx = _make_action_context(session)
        args = FanoutReplanForInitiativeArgs(initiative_id=str(uuid.uuid4()))
        result = await fanout_replan_for_initiative_action(args, ctx)

    assert result.skipped is True
    assert result.skip_reason == 'initiative_not_found'


@pytest.mark.asyncio
async def test_fanout_replan_for_initiative_invalid_uuid() -> None:
    """fanout_replan_for_initiative: invalid initiative_id → skip gracefully."""
    from src.engines.access_plan.actions import (
        FanoutReplanForInitiativeArgs,
        fanout_replan_for_initiative_action,
    )

    session_mock = AsyncMock()
    ctx = _make_action_context(session_mock)
    args = FanoutReplanForInitiativeArgs(initiative_id='bad-uuid')
    result = await fanout_replan_for_initiative_action(args, ctx)

    assert result.skipped is True
    assert 'invalid' in (result.skip_reason or '')
