# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Idempotency tests: re-running migration must not create duplicate delta items or Iceberg rows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from src.capabilities.lake_migration.models import LakeMigrationDataset
from src.capabilities.lake_migration.service import LakeMigrationService
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.logs.service import NoOpLogService
from src.platform.storage.factory import DataLakeStorageFactory


def _svc() -> LakeMigrationService:
    return LakeMigrationService(
        log_service=NoOpLogService(),
        lake_batch_service=LakeBatchService(storage_factory=DataLakeStorageFactory(), log_service=NoOpLogService()),
    )


async def _create_app_and_facts(session, count: int):
    """Seed app + subject + resource + N facts via raw SQL for access_facts."""
    import uuid as _uuid  # noqa: PLC0415

    from src.inventory.actions.models import Action  # noqa: PLC0415
    from src.inventory.nhi.models import NHI  # noqa: PLC0415
    from src.inventory.resources.models import Resource  # noqa: PLC0415
    from src.inventory.subjects.models import Subject  # noqa: PLC0415
    from src.platform.applications.models import Application  # noqa: PLC0415

    app = Application(name='Test', code='test-idem')
    session.add(app)
    await session.flush()

    nhi = NHI(external_id='svc-u1', name='NHI u1', kind='service_account')
    session.add(nhi)
    await session.flush()

    subj = Subject(
        external_id='u1',
        kind='nhi',
        nhi_kind='service_account',
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subj)
    await session.flush()

    res = Resource(
        application_id=app.id,
        external_id='r1',
        kind='role',
        resource_type='role_assignment',
        resource_key='r1-key',
    )
    session.add(res)
    await session.flush()

    result = await session.execute(sa.select(Action.id).where(Action.slug == 'read'))
    action_id = result.scalar_one_or_none()
    if action_id is None:
        a = Action(slug='read', description='Read')
        session.add(a)
        await session.flush()
        action_id = a.id

    for i in range(count):
        if i == 0:
            r_id = res.id
        else:
            sibling = Resource(
                application_id=app.id,
                external_id=f'r-idem-{i}',
                kind='role',
                resource_type='role_assignment',
                resource_key=f'r-idem-key-{i}',
            )
            session.add(sibling)
            await session.flush()
            r_id = sibling.id

        await session.execute(
            sa.text(
                'INSERT INTO access_facts '
                '(id, subject_id, resource_id, action_id, effect, valid_from, observed_at, is_active) '
                'VALUES (:id, :subject_id, :resource_id, :action_id, :effect, :valid_from, :observed_at, :is_active)'
            ),
            {
                'id': _uuid.uuid4(),
                'subject_id': subj.id,
                'resource_id': r_id,
                'action_id': action_id,
                'effect': 'allow',
                'valid_from': datetime(2026, 1, 1, tzinfo=UTC),
                'observed_at': datetime(2026, 1, 1, tzinfo=UTC),
                'is_active': True,
            },
        )
    await session.flush()


@pytest.mark.asyncio
async def test_idempotent_facts_migration(db_session, session_factory, catalog, lake_session) -> None:
    """Running facts migration twice on same dataset must not duplicate delta items."""
    from src.capabilities.lake_migration.models import LakeMigrationRun  # noqa: PLC0415
    from src.capabilities.reconciliation.models import ReconciliationDeltaItem  # noqa: PLC0415

    svc = _svc()
    await _create_app_and_facts(db_session, count=10)
    await db_session.commit()

    # First run.
    async with session_factory() as s:
        run1 = await svc.start_migration(s, dataset=LakeMigrationDataset.access_facts, batch_size=50)
        await s.commit()

    async with session_factory() as s:
        r = await s.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run1.id))
        run1_r = r.scalar_one()
        await svc.migrate_access_facts(s, run1_r, lake_session=lake_session, catalog=catalog)
        await s.commit()

    # Second run (new run_id, same source).
    async with session_factory() as s:
        run2 = await svc.start_migration(s, dataset=LakeMigrationDataset.access_facts, batch_size=50)
        await s.commit()

    async with session_factory() as s:
        r = await s.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run2.id))
        run2_r = r.scalar_one()
        await svc.migrate_access_facts(s, run2_r, lake_session=lake_session, catalog=catalog)
        await s.commit()

    # Delta items must not be duplicated.
    async with session_factory() as s:
        result = await s.execute(
            sa.select(ReconciliationDeltaItem).where(ReconciliationDeltaItem.reason == 'pg_migration')
        )
        items = result.scalars().all()

    # 10 delta items total (idempotent — no duplicates on second run).
    assert len(items) == 10
