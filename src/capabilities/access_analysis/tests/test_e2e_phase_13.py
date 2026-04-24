# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase 13 end-to-end integration test.

Single test function that walks a complete scenario through the public REST API:
  Stage 1  — DB-level bootstrap (EffectiveGrant + upstream rows)
  Stage 2  — Capability vocabulary (POST /capabilities, POST /capability-mappings)
  Stage 3  — SoD rule + conditions
  Stage 4  — Capability projection (direct service call; no HTTP endpoint exists)
  Stage 5  — Scan run (create + run)
  Stage 6  — Verify finding (kind=sod, status=open, severity=high, scope_value=le-001)
  Stage 7  — Mitigation (control + mitigation record, active, valid_from=_NOW-1h)
  Stage 8  — Re-scan; original finding row becomes status=mitigated, counts correct
  Stage 9  — Feedback (accepted_risk) on the finding
  Stage 10 — On-demand /sod/evaluate: mitigated at _NOW, not mitigated at _NOW-2h
  Stage 11 — On-demand /sod/what-if for owner subject; violation returned, nothing persisted
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.capability_grants.service import CapabilityProjectionService
from src.capabilities.effective_access.models import EffectiveGrant, EffectiveGrantEffect
from src.inventory.access_facts.models import AccessFact, AccessFactEffect
from src.inventory.actions.models import Action as RefAction
from src.inventory.enums import Action
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application

_NOW = datetime(2026, 4, 24, 12, 0, tzinfo=UTC)
_SCOPE_VALUE = 'le-001'


# ---------------------------------------------------------------------------
# Stage-level helpers (module-private)
# ---------------------------------------------------------------------------


async def _make_employee_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Create a minimal employee subject. Returns subject.id."""
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person

    person = await create_person(session, external_id=str(uuid.uuid4()), description='e2e test person')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _seed_inventory(session) -> dict:  # type: ignore[no-untyped-def]
    """Stage 1: insert all upstream rows needed by subsequent stages.

    Returns a dict with all IDs needed by the test body.
    """
    valid_from = _NOW - timedelta(hours=1)

    # GLOBAL scope key (required by CapabilityProjectionService — seed migration not run in tests)
    from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey

    existing_global = (
        await session.execute(sa.select(CapabilityScopeKey).where(CapabilityScopeKey.code == 'GLOBAL'))
    ).scalar_one_or_none()
    if existing_global is None:
        global_sk = CapabilityScopeKey(code='GLOBAL', name='Global')
        session.add(global_sk)
        await session.flush()

    # Application
    app = Application(
        name=f'e2e-app-{uuid.uuid4().hex[:8]}',
        code=f'e2e-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    # Actor subject (employee — partition key for EG rows)
    actor_id = await _make_employee_subject(session)

    # Owner subject (second employee — used as mitigation owner)
    owner_id = await _make_employee_subject(session)

    # Resource 1 (for EG 1 / create_vendor capability mapping)
    resource_1 = Resource(
        external_id=f'e2e-res1-{uuid.uuid4().hex[:8]}',
        application_id=app.id,
        kind='role',
        resource_type='role',
        resource_key=f'e2e-key1-{uuid.uuid4().hex[:8]}',
    )
    session.add(resource_1)
    await session.flush()

    # Resource 2 (for EG 2 / approve_payment capability mapping)
    resource_2 = Resource(
        external_id=f'e2e-res2-{uuid.uuid4().hex[:8]}',
        application_id=app.id,
        kind='role',
        resource_type='role',
        resource_key=f'e2e-key2-{uuid.uuid4().hex[:8]}',
    )
    session.add(resource_2)
    await session.flush()

    # Ref action (read — exists in both ref_actions seed and Action enum)
    read_action_id = (await session.execute(sa.select(RefAction.id).where(RefAction.slug == 'read'))).scalar_one()

    # AccessFact 1 — actor, resource_1, read
    fact_1 = AccessFact(
        subject_id=actor_id,
        resource_id=resource_1.id,
        action_id=read_action_id,
        effect=AccessFactEffect.allow,
        observed_at=valid_from,
        valid_from=valid_from,
        is_active=True,
    )
    session.add(fact_1)
    await session.flush()

    # AccessFact 2 — actor, resource_2, read
    fact_2 = AccessFact(
        subject_id=actor_id,
        resource_id=resource_2.id,
        action_id=read_action_id,
        effect=AccessFactEffect.allow,
        observed_at=valid_from,
        valid_from=valid_from,
        is_active=True,
    )
    session.add(fact_2)
    await session.flush()

    # Initiative (shared by both facts / EGs)
    initiative = Initiative(
        access_fact_id=fact_1.id,
        type=InitiativeType.birthright,
        origin='e2e:phase_13',
        valid_from=valid_from,
        valid_until=None,
    )
    session.add(initiative)
    await session.flush()

    # EffectiveGrant 1 (fact_1, resource_1)
    eg_id_1 = uuid.uuid4()
    eg_1 = EffectiveGrant(
        id=eg_id_1,
        subject_id=actor_id,
        subject_kind=SubjectKind.employee,
        application_id=app.id,
        resource_id=resource_1.id,
        action=Action.read,
        effect=EffectiveGrantEffect.allow,
        initiative_type=InitiativeType.birthright,
        initiative_origin='e2e:phase_13',
        valid_from=valid_from,
        valid_until=None,
        source_access_fact_id=fact_1.id,
        source_initiative_id=initiative.id,
        tombstoned_at=None,
    )
    session.add(eg_1)
    await session.flush()

    # EffectiveGrant 2 (fact_2, resource_2)
    eg_id_2 = uuid.uuid4()
    eg_2 = EffectiveGrant(
        id=eg_id_2,
        subject_id=actor_id,
        subject_kind=SubjectKind.employee,
        application_id=app.id,
        resource_id=resource_2.id,
        action=Action.read,
        effect=EffectiveGrantEffect.allow,
        initiative_type=InitiativeType.birthright,
        initiative_origin='e2e:phase_13',
        valid_from=valid_from,
        valid_until=None,
        source_access_fact_id=fact_2.id,
        source_initiative_id=initiative.id,
        tombstoned_at=None,
    )
    session.add(eg_2)
    await session.flush()

    return {
        'app_id': app.id,
        'actor_id': actor_id,
        'owner_id': owner_id,
        'resource_1_id': resource_1.id,
        'resource_2_id': resource_2.id,
        'eg_id_1': eg_id_1,
        'eg_id_2': eg_id_2,
        # Pass resource IDs to Stage 2 so mappings can match by resource_id (not resource_kind)
        'resource_1_external_id': resource_1.external_id,
        'resource_2_external_id': resource_2.external_id,
    }


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_13_end_to_end(client, session_factory) -> None:
    """Full Phase 13 pipeline: vocabulary → projection → scan → mitigation → evaluate → what-if."""

    # -----------------------------------------------------------------------
    # Stage 1 — Inventory bootstrap
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        refs = await _seed_inventory(session)
        await session.commit()

    actor_id = refs['actor_id']
    owner_id = refs['owner_id']
    eg_id_1 = refs['eg_id_1']
    eg_id_2 = refs['eg_id_2']

    # -----------------------------------------------------------------------
    # Stage 2 — Capability vocabulary via HTTP
    # -----------------------------------------------------------------------

    # Scope key LEGAL_ENTITY (not in seed, must be created)
    sk_resp = await client.post(
        '/api/v0/capability-scope-keys',
        json={'code': 'LEGAL_ENTITY', 'name': 'Legal Entity'},
    )
    assert sk_resp.status_code == 201, sk_resp.text
    scope_key_id: int = sk_resp.json()['id']

    # Capability 1: create_vendor
    cap1_resp = await client.post(
        '/api/v0/capabilities',
        json={'slug': 'create_vendor', 'name': 'Create Vendor'},
    )
    assert cap1_resp.status_code == 201, cap1_resp.text
    cap1_id: int = cap1_resp.json()['id']

    # Capability 2: approve_payment
    cap2_resp = await client.post(
        '/api/v0/capabilities',
        json={'slug': 'approve_payment', 'name': 'Approve Payment'},
    )
    assert cap2_resp.status_code == 201, cap2_resp.text
    cap2_id: int = cap2_resp.json()['id']

    resource_1_id = refs['resource_1_id']
    resource_2_id = refs['resource_2_id']

    # CapabilityMapping 1 — matches resource_1 specifically (EG_1 → create_vendor)
    map1_resp = await client.post(
        '/api/v0/capability-mappings',
        json={
            'capability_id': cap1_id,
            'resource_id': str(resource_1_id),
            'scope_key_id': scope_key_id,
            'scope_value_source': {'kind': 'constant', 'value': _SCOPE_VALUE},
            'is_active': True,
        },
    )
    assert map1_resp.status_code == 201, map1_resp.text

    # CapabilityMapping 2 — matches resource_2 specifically (EG_2 → approve_payment)
    map2_resp = await client.post(
        '/api/v0/capability-mappings',
        json={
            'capability_id': cap2_id,
            'resource_id': str(resource_2_id),
            'scope_key_id': scope_key_id,
            'scope_value_source': {'kind': 'constant', 'value': _SCOPE_VALUE},
            'is_active': True,
        },
    )
    assert map2_resp.status_code == 201, map2_resp.text

    # -----------------------------------------------------------------------
    # Stage 3 — SoD rule + two conditions
    # -----------------------------------------------------------------------

    rule_resp = await client.post(
        '/api/v0/sod-rules',
        json={
            'code': 'E2E_SOD_VENDOR_PAYMENT',
            'name': 'E2E: Vendor + Payment SoD',
            'severity': 'high',
            'scope_mode': 'by_scope_key',
            'scope_key_id': scope_key_id,
            'mitigation_allowed': True,
            'is_enabled': True,
        },
    )
    assert rule_resp.status_code == 201, rule_resp.text
    rule_id: int = rule_resp.json()['id']

    # Condition 1 — requires create_vendor
    cond1_resp = await client.post(
        f'/api/v0/sod-rules/{rule_id}/conditions',
        json={'capability_ids': [cap1_id], 'min_count': 1},
    )
    assert cond1_resp.status_code == 201, cond1_resp.text

    # Condition 2 — requires approve_payment
    cond2_resp = await client.post(
        f'/api/v0/sod-rules/{rule_id}/conditions',
        json={'capability_ids': [cap2_id], 'min_count': 1},
    )
    assert cond2_resp.status_code == 201, cond2_resp.text

    # -----------------------------------------------------------------------
    # Stage 4 — Capability projection (direct service call; no HTTP endpoint)
    # -----------------------------------------------------------------------

    # Project at _NOW - 3h so that CapabilityGrant.observed_at is strictly before _NOW - 2h.
    # This satisfies the active-at predicate (observed_at <= at) for all three evaluation
    # points used in Stage 10: _NOW - 2h (before mitigation), _NOW (after mitigation).
    _PROJECTION_AT = _NOW - timedelta(hours=3)

    async with session_factory() as session:
        svc = CapabilityProjectionService(session)
        await svc.project_for_effective_grant(effective_grant_id=eg_id_1, now=_PROJECTION_AT)
        await svc.project_for_effective_grant(effective_grant_id=eg_id_2, now=_PROJECTION_AT)
        await session.commit()

    grants_resp = await client.get(f'/api/v0/capability-grants?subject_id={actor_id}')
    assert grants_resp.status_code == 200, grants_resp.text
    grants = grants_resp.json()
    assert len(grants) == 2, f'Expected 2 capability grants, got {len(grants)}: {grants}'
    grant_scope_values = {g['scope_value'] for g in grants}
    assert grant_scope_values == {_SCOPE_VALUE}, f'scope_value mismatch: {grant_scope_values}'

    # -----------------------------------------------------------------------
    # Stage 5 — Scan run: create + execute
    # -----------------------------------------------------------------------

    create_run_resp = await client.post(
        '/api/v0/scan-runs',
        json={'triggered_by': 'api'},
    )
    assert create_run_resp.status_code == 201, create_run_resp.text
    run_id: int = create_run_resp.json()['id']

    run_resp = await client.post(f'/api/v0/scan-runs/{run_id}/run')
    assert run_resp.status_code == 200, run_resp.text
    run_data = run_resp.json()
    assert run_data['status'] == 'completed', f'Expected completed, got {run_data["status"]}'
    assert run_data['findings_created_count'] == 1, run_data
    assert run_data['findings_reused_count'] == 0, run_data

    # -----------------------------------------------------------------------
    # Stage 6 — Findings: exactly one sod finding, open, correct fields
    # -----------------------------------------------------------------------

    findings_resp = await client.get(f'/api/v0/findings?scan_run_id={run_id}')
    assert findings_resp.status_code == 200, findings_resp.text
    findings = findings_resp.json()
    assert len(findings) == 1, f'Expected 1 finding, got {len(findings)}: {findings}'

    finding = findings[0]
    finding_id: int = finding['id']

    assert finding['kind'] == 'sod'
    assert finding['status'] == 'open'
    assert finding['rule_id'] == rule_id
    assert finding['scope_value'] == _SCOPE_VALUE
    assert finding['severity'] == 'high'
    assert finding['active_mitigation_id'] is None
    assert finding['proposed_mitigation_id'] is None
    assert len(finding['matched_capability_grant_ids']) == 2

    # -----------------------------------------------------------------------
    # Stage 7 — Mitigation: control + mitigation record
    # -----------------------------------------------------------------------

    ctrl_resp = await client.post(
        '/api/v0/mitigation-controls',
        json={
            'code': 'E2E_PHASE_13_CONTROL',
            'name': 'Phase 13 e2e test control',
            'type': 'attestation',
        },
    )
    assert ctrl_resp.status_code == 201, ctrl_resp.text
    control_id: int = ctrl_resp.json()['id']

    mitigation_valid_from = (_NOW - timedelta(hours=1)).isoformat()
    mitigation_valid_until = (_NOW + timedelta(days=1)).isoformat()

    mit_resp = await client.post(
        '/api/v0/mitigations',
        json={
            'rule_id': rule_id,
            'control_id': control_id,
            'subject_id': str(actor_id),
            'scope_key_id': scope_key_id,
            'scope_value': _SCOPE_VALUE,
            'status': 'active',
            'valid_from': mitigation_valid_from,
            'valid_until': mitigation_valid_until,
            'owner_id': str(owner_id),
        },
    )
    assert mit_resp.status_code == 201, mit_resp.text
    mitigation_id: int = mit_resp.json()['id']

    # -----------------------------------------------------------------------
    # Stage 8 — Re-scan: finding reused, original row mutated to mitigated
    # -----------------------------------------------------------------------

    create_run2_resp = await client.post(
        '/api/v0/scan-runs',
        json={'triggered_by': 'api'},
    )
    assert create_run2_resp.status_code == 201, create_run2_resp.text
    run2_id: int = create_run2_resp.json()['id']

    run2_resp = await client.post(f'/api/v0/scan-runs/{run2_id}/run')
    assert run2_resp.status_code == 200, run2_resp.text
    run2_data = run2_resp.json()
    assert run2_data['status'] == 'completed', run2_data
    assert run2_data['findings_created_count'] == 0, run2_data
    assert run2_data['findings_reused_count'] == 1, run2_data

    # Original finding must now be mitigated, with same id
    findings_after_resp = await client.get(f'/api/v0/findings?scan_run_id={run_id}')
    assert findings_after_resp.status_code == 200, findings_after_resp.text
    findings_after = findings_after_resp.json()
    assert len(findings_after) == 1
    finding_after = findings_after[0]
    assert finding_after['id'] == finding_id, 'Finding id must not change'
    assert finding_after['status'] == 'mitigated'
    assert finding_after['active_mitigation_id'] == mitigation_id

    # -----------------------------------------------------------------------
    # Stage 9 — Feedback on the finding
    # -----------------------------------------------------------------------

    fb_resp = await client.post(
        '/api/v0/feedbacks',
        json={
            'finding_id': finding_id,
            'kind': 'accepted_risk',
            'message': 'E2E phase 13: accepted risk for vendor + payment SoD.',
        },
    )
    assert fb_resp.status_code == 201, fb_resp.text
    fb_id: int = fb_resp.json()['id']

    fb_list_resp = await client.get(f'/api/v0/feedbacks?finding_id={finding_id}')
    assert fb_list_resp.status_code == 200, fb_list_resp.text
    fb_list = fb_list_resp.json()
    assert any(f['id'] == fb_id for f in fb_list)

    # -----------------------------------------------------------------------
    # Stage 10 — On-demand /sod/evaluate: mitigated at _NOW, not at _NOW-2h
    # -----------------------------------------------------------------------

    eval_now_resp = await client.post(
        '/api/v0/sod/evaluate',
        json={'subject_id': str(actor_id), 'at': _NOW.isoformat()},
    )
    assert eval_now_resp.status_code == 200, eval_now_resp.text
    violations_now = eval_now_resp.json()
    assert len(violations_now) == 1, f'Expected 1 violation at _NOW, got {violations_now}'
    v_now = violations_now[0]
    assert v_now['is_mitigated'] is True
    assert v_now['active_mitigation_id'] == mitigation_id
    assert v_now['rule_id'] == rule_id
    assert v_now['scope_value'] == _SCOPE_VALUE

    # Before mitigation valid_from (_NOW - 2h < _NOW - 1h): should be unmitigated
    before_mit = (_NOW - timedelta(hours=2)).isoformat()
    eval_before_resp = await client.post(
        '/api/v0/sod/evaluate',
        json={'subject_id': str(actor_id), 'at': before_mit},
    )
    assert eval_before_resp.status_code == 200, eval_before_resp.text
    violations_before = eval_before_resp.json()
    assert len(violations_before) == 1, f'Expected 1 violation before mitigation, got {violations_before}'
    v_before = violations_before[0]
    assert v_before['is_mitigated'] is False
    assert v_before['active_mitigation_id'] is None

    # -----------------------------------------------------------------------
    # Stage 11 — /sod/what-if for owner subject; must not persist
    # -----------------------------------------------------------------------

    whatif_resp = await client.post(
        '/api/v0/sod/what-if',
        json={
            'subject_id': str(owner_id),
            'at': _NOW.isoformat(),
            'capability_overrides': [
                {
                    'capability_id': cap1_id,
                    'scope_key_id': scope_key_id,
                    'scope_value': _SCOPE_VALUE,
                    'application_id': str(refs['app_id']),
                },
                {
                    'capability_id': cap2_id,
                    'scope_key_id': scope_key_id,
                    'scope_value': _SCOPE_VALUE,
                    'application_id': str(refs['app_id']),
                },
            ],
        },
    )
    assert whatif_resp.status_code == 200, whatif_resp.text
    hypothetical = whatif_resp.json()
    assert len(hypothetical) == 1, f'Expected 1 hypothetical violation, got {hypothetical}'
    h = hypothetical[0]
    assert h['is_mitigated'] is False  # no mitigation exists for owner

    # What-if must NOT have persisted any findings for owner
    owner_findings_resp = await client.get(f'/api/v0/findings?subject_id={owner_id}')
    assert owner_findings_resp.status_code == 200, owner_findings_resp.text
    assert owner_findings_resp.json() == [], 'what-if must not persist findings'
