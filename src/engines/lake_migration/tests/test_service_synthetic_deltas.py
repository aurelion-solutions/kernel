# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for synthetic ReconciliationRun + ReconciliationDeltaItem produced during facts migration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from src.engines.lake_migration.models import LakeMigrationDataset
from src.engines.lake_migration.service import LakeMigrationService
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.logs.service import NoOpLogService
from src.platform.storage.factory import DataLakeStorageFactory


def _svc() -> LakeMigrationService:
    return LakeMigrationService(
        log_service=NoOpLogService(),
        lake_batch_service=LakeBatchService(storage_factory=DataLakeStorageFactory(), log_service=NoOpLogService()),
    )


async def _seed_single_fact(session):
    """Seed one access_fact via raw SQL — ORM model deleted Phase 15 Step 16."""
    import uuid as _uuid  # noqa: PLC0415

    from src.inventory.actions.models import Action  # noqa: PLC0415
    from src.inventory.nhi.models import NHI  # noqa: PLC0415
    from src.inventory.resources.models import Resource  # noqa: PLC0415
    from src.inventory.subjects.models import Subject  # noqa: PLC0415
    from src.platform.applications.models import Application  # noqa: PLC0415

    app = Application(name='SynthTest', code='synth-test')
    session.add(app)
    await session.flush()

    nhi = NHI(external_id='svc-synth', name='NHI synth', kind='service_account')
    session.add(nhi)
    await session.flush()

    subj = Subject(
        external_id='u-synth',
        kind='nhi',
        nhi_kind='service_account',
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subj)
    await session.flush()

    res = Resource(
        application_id=app.id,
        external_id='res-synth',
        kind='role',
        resource_type='role_assignment',
        resource_key='res-synth-key',
    )
    session.add(res)
    await session.flush()

    r = await session.execute(sa.select(Action.id).where(Action.slug == 'read'))
    action_id = r.scalar_one_or_none()
    if action_id is None:
        a = Action(slug='read', description='Read')
        session.add(a)
        await session.flush()
        action_id = a.id

    fact_id = _uuid.uuid4()
    await session.execute(
        sa.text(
            'INSERT INTO access_facts '
            '(id, subject_id, resource_id, action_id, effect, valid_from, observed_at, is_active) '
            'VALUES (:id, :subject_id, :resource_id, :action_id, :effect, :valid_from, :observed_at, :is_active)'
        ),
        {
            'id': fact_id,
            'subject_id': subj.id,
            'resource_id': res.id,
            'action_id': action_id,
            'effect': 'allow',
            'valid_from': datetime(2026, 3, 1, tzinfo=UTC),
            'observed_at': datetime(2026, 3, 1, tzinfo=UTC),
            'is_active': True,
        },
    )
    await session.flush()

    # Return a simple namespace object with .id attribute for backward compat
    class _FactProxy:
        def __init__(self, fid: _uuid.UUID) -> None:
            self.id = fid

    return _FactProxy(fact_id), app.id


@pytest.mark.asyncio
async def test_synthetic_run_has_null_application_id(db_session, session_factory, catalog, lake_session) -> None:
    """Synthetic ReconciliationRun must have application_id=NULL."""
    from src.engines.lake_migration.models import LakeMigrationRun  # noqa: PLC0415
    from src.engines.reconciliation.models import ReconciliationRun  # noqa: PLC0415

    svc = _svc()
    fact, _ = await _seed_single_fact(db_session)
    await db_session.commit()

    async with session_factory() as s:
        run = await svc.start_migration(s, dataset=LakeMigrationDataset.access_facts, batch_size=50)
        await s.commit()

    async with session_factory() as s:
        r = await s.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run.id))
        run_r = r.scalar_one()
        await svc.migrate_access_facts(s, run_r, lake_session=lake_session, catalog=catalog)
        await s.commit()

    async with session_factory() as s:
        r = await s.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run.id))
        run_final = r.scalar_one()
        assert run_final.synthetic_run_id is not None
        synth_r = await s.execute(
            sa.select(ReconciliationRun).where(ReconciliationRun.id == run_final.synthetic_run_id)
        )
        synth_run = synth_r.scalar_one()
        assert synth_run.application_id is None
        assert synth_run.status.value == 'applied'


@pytest.mark.asyncio
async def test_delta_item_shape(db_session, session_factory, catalog, lake_session) -> None:
    """Verify delta item has correct operation, status, before_json=NULL, after_json shape."""
    from src.engines.lake_migration.models import LakeMigrationRun  # noqa: PLC0415
    from src.engines.reconciliation.models import ReconciliationDeltaItem  # noqa: PLC0415

    svc = _svc()
    fact, _ = await _seed_single_fact(db_session)
    await db_session.commit()

    async with session_factory() as s:
        run = await svc.start_migration(s, dataset=LakeMigrationDataset.access_facts, batch_size=50)
        await s.commit()

    async with session_factory() as s:
        r = await s.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run.id))
        run_r = r.scalar_one()
        await svc.migrate_access_facts(s, run_r, lake_session=lake_session, catalog=catalog)
        await s.commit()

    async with session_factory() as s:
        r = await s.execute(
            sa.select(ReconciliationDeltaItem).where(
                ReconciliationDeltaItem.reason == 'pg_migration',
                ReconciliationDeltaItem.existing_fact_id == fact.id,
            )
        )
        item = r.scalar_one()

    assert item.operation.value == 'create'
    assert item.status.value == 'applied'
    assert item.before_json is None
    assert item.source_artifact_id is None
    assert item.existing_fact_id == fact.id
    # after_json must have 'origin' key.
    assert item.after_json is not None
    assert item.after_json.get('origin') == 'pg_migration'
    assert item.after_json.get('fact_id') == str(fact.id)
    assert 'effect' in item.after_json
