# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for CapabilityProjectionService and CapabilityGrantReadService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.capability_grants.exceptions import (
    EffectiveGrantNotFoundForProjectionError,
)
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_grants.service import (
    CapabilityProjectionService,
)
from src.capabilities.access_analysis.capability_grants.tests import _seed_minimal_refs

_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_project_for_effective_grant_inserts_capability_grant(session_factory) -> None:
    """Happy path: project one EG → one CapabilityGrant row inserted."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)
        service = CapabilityProjectionService(session)

        summary = await service.project_for_effective_grant(
            effective_grant_id=refs.effective_grant_id,
            now=_NOW,
        )

        assert summary.pairs_projected == 1
        assert summary.rows_inserted == 1
        assert summary.rows_updated == 0

        rows = (await session.execute(sa.select(CapabilityGrant))).scalars().all()
        assert len(rows) == 1
        assert rows[0].source_effective_grant_id == refs.effective_grant_id
        assert rows[0].capability_id == refs.capability_id


@pytest.mark.asyncio
async def test_project_for_eg_unknown_id_raises_not_found_for_projection_error(  # noqa: E501
    session_factory,
) -> None:
    """Unknown EG id raises EffectiveGrantNotFoundForProjectionError."""
    async with session_factory() as session:
        await _seed_minimal_refs(session)
        service = CapabilityProjectionService(session)

        with pytest.raises(EffectiveGrantNotFoundForProjectionError) as exc_info:
            await service.project_for_effective_grant(
                effective_grant_id=uuid.uuid4(),
                now=_NOW,
            )
        assert exc_info.value.effective_grant_id is not None


@pytest.mark.asyncio
async def test_re_projection_does_not_overwrite_application_id(session_factory) -> None:
    """Project once; mutate EG's application_id via SQL; re-project; assert CapabilityGrant.application_id unchanged."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)
        service = CapabilityProjectionService(session)
        original_app_id = refs.application_id

        # First projection
        await service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=_NOW)

        # Mutate EG's application_id via direct SQL (test-only — simulates a data fix)
        from src.platform.applications.models import Application

        app2 = Application(
            name=f'app2-{uuid.uuid4().hex[:8]}',
            code=f'code2-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app2)
        await session.flush()

        from src.capabilities.effective_access.models import EffectiveGrant

        await session.execute(
            sa.update(EffectiveGrant).where(EffectiveGrant.id == refs.effective_grant_id).values(application_id=app2.id)
        )
        await session.flush()

        # Re-project
        t2 = _NOW + timedelta(hours=1)
        await service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=t2)

        row = (await session.execute(sa.select(CapabilityGrant))).scalars().one()
        assert row.application_id == original_app_id  # must NOT be app2.id


@pytest.mark.asyncio
async def test_tombstoned_effective_grant_yields_tombstoned_capability_grant(session_factory) -> None:
    """EG tombstoned → projected CapabilityGrant.tombstoned_at is populated."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        from src.capabilities.effective_access.models import EffectiveGrant

        tombstone_ts = _NOW - timedelta(hours=1)
        await session.execute(
            sa.update(EffectiveGrant)
            .where(EffectiveGrant.id == refs.effective_grant_id)
            .values(tombstoned_at=tombstone_ts)
        )
        await session.flush()

        service = CapabilityProjectionService(session)
        await service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=_NOW)

        row = (await session.execute(sa.select(CapabilityGrant))).scalars().one()
        assert row.tombstoned_at == tombstone_ts


@pytest.mark.asyncio
async def test_project_for_application_processes_all_grants(session_factory) -> None:
    """Seed 3 EGs in one application; one call → 3 grants persisted."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        # Add 2 more EGs in the same application — use distinct resources to avoid UQ violation
        import uuid as _uuid

        from src.capabilities.effective_access.models import EffectiveGrant, EffectiveGrantEffect
        from src.inventory.actions.models import Action as RefAction
        from src.inventory.enums import Action
        from src.inventory.initiatives.models import Initiative, InitiativeType
        from src.inventory.resources.models import Resource
        from src.inventory.subjects.models import SubjectKind

        now = _NOW
        read_action_id = (await session.execute(sa.select(RefAction.id).where(RefAction.slug == 'read'))).scalar_one()

        for _ in range(2):
            # New resource per EG to avoid uq_access_facts_active_subject_key violation
            extra_resource = Resource(
                external_id=f'ext-extra-{_uuid.uuid4().hex[:8]}',
                application_id=refs.application_id,
                kind='role',
                resource_type='role',
                resource_key=f'key-extra-{_uuid.uuid4().hex[:8]}',
            )
            session.add(extra_resource)
            await session.flush()

            fact_id = _uuid.uuid4()
            await session.execute(
                sa.text(
                    'INSERT INTO access_facts '
                    '(id, subject_id, resource_id, action_id, effect, observed_at, valid_from) '
                    'VALUES (:id, :subject_id, :resource_id, :action_id, :effect, :observed_at, :valid_from)'
                ),
                {
                    'id': fact_id,
                    'subject_id': refs.subject_id,
                    'resource_id': extra_resource.id,
                    'action_id': read_action_id,
                    'effect': 'allow',
                    'observed_at': now,
                    'valid_from': now,
                },
            )
            await session.flush()

            initiative = Initiative(
                access_fact_id=fact_id,
                type=InitiativeType.birthright,
                origin='test',
                valid_from=now,
                valid_until=None,
            )
            session.add(initiative)
            await session.flush()

            eg = EffectiveGrant(
                id=_uuid.uuid4(),
                subject_id=refs.subject_id,
                subject_kind=SubjectKind.nhi,
                application_id=refs.application_id,
                resource_id=extra_resource.id,
                action=Action.read,
                effect=EffectiveGrantEffect.allow,
                initiative_type=InitiativeType.birthright,
                initiative_origin='test',
                valid_from=now,
                valid_until=None,
                source_access_fact_id=fact_id,
                source_initiative_id=initiative.id,
                observed_at=now,
                tombstoned_at=None,
            )
            session.add(eg)
            await session.flush()

        service = CapabilityProjectionService(session)
        summary = await service.project_for_application(
            application_id=refs.application_id,
            now=_NOW,
        )

        # 3 EGs: the one from _seed_minimal_refs (resource_kind='role' matches mapping)
        # + 2 extra resources (also kind='role', also match)
        assert summary.pairs_projected == 3
        assert summary.rows_inserted == 3

        rows = (await session.execute(sa.select(CapabilityGrant))).scalars().all()
        assert len(rows) == 3


@pytest.mark.asyncio
async def test_global_scope_key_yields_null_scope_value(session_factory) -> None:
    """Mapping with GLOBAL scope key and constant source → projected grant has scope_value=None."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)
        # The seeded mapping already uses GLOBAL scope key with constant source 'admin'
        service = CapabilityProjectionService(session)

        await service.project_for_effective_grant(effective_grant_id=refs.effective_grant_id, now=_NOW)

        row = (await session.execute(sa.select(CapabilityGrant))).scalars().one()
        assert row.scope_value is None  # GLOBAL sentinel
        assert row.scope_key_id == refs.scope_key_global_id


@pytest.mark.asyncio
async def test_no_active_mappings_yields_empty_run(session_factory) -> None:
    """Seed EG, deactivate all mappings → zero drafts, zero rows. pairs_projected=0."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping

        await session.execute(sa.update(CapabilityMapping).values(is_active=False))
        await session.flush()

        service = CapabilityProjectionService(session)
        summary = await service.project_for_effective_grant(
            effective_grant_id=refs.effective_grant_id,
            now=_NOW,
        )

        assert summary.pairs_projected == 0
        assert summary.rows_upserted == 0

        rows = (await session.execute(sa.select(CapabilityGrant))).scalars().all()
        assert len(rows) == 0
