# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-level tests for lake_migration (artifacts + facts happy paths)."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.lake_migration.models import LakeMigrationDataset, LakeMigrationStatus
from src.engines.lake_migration.service import LakeMigrationService
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.logs.service import NoOpLogService
from src.platform.storage.factory import DataLakeStorageFactory


def _make_service() -> LakeMigrationService:
    return LakeMigrationService(
        log_service=NoOpLogService(),
        lake_batch_service=LakeBatchService(
            storage_factory=DataLakeStorageFactory(),
            log_service=NoOpLogService(),
        ),
    )


async def _seed_application(session: AsyncSession) -> uuid.UUID:
    from src.platform.applications.models import Application  # noqa: PLC0415

    app = Application(name='Test App', code='test-app')
    session.add(app)
    await session.flush()
    return app.id


async def _seed_subject(session: AsyncSession) -> uuid.UUID:
    from src.inventory.nhi.models import NHI  # noqa: PLC0415
    from src.inventory.subjects.models import Subject  # noqa: PLC0415

    nhi = NHI(external_id='svc-user1', name='Service Account', kind='service_account')
    session.add(nhi)
    await session.flush()

    s = Subject(
        external_id='user1',
        kind='nhi',
        nhi_kind='service_account',
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(s)
    await session.flush()
    return s.id


async def _seed_resource(session: AsyncSession, app_id: uuid.UUID) -> uuid.UUID:
    from src.inventory.resources.models import Resource  # noqa: PLC0415

    r = Resource(
        application_id=app_id,
        external_id='res1',
        kind='role',
        resource_type='role_assignment',
        resource_key='res1-key',
    )
    session.add(r)
    await session.flush()
    return r.id


async def _seed_artifacts(
    session: AsyncSession,
    app_id: uuid.UUID,
    count: int,
) -> list[uuid.UUID]:
    """Seed access_artifacts via raw SQL — ORM model deleted Phase 15 Step 16."""
    ids = []
    for i in range(count):
        row_id = uuid.uuid4()
        await session.execute(
            sa.text(
                'INSERT INTO access_artifacts '
                '(id, application_id, artifact_type, external_id, payload, observed_at, is_active) '
                'VALUES (:id, :app_id, :artifact_type, :ext_id, :payload, :observed_at, :is_active)'
            ),
            {
                'id': row_id,
                'app_id': app_id,
                'artifact_type': 'role_assignment',
                'ext_id': f'ext-{i}',
                'payload': '{"key": "v' + str(i) + '"}',
                'observed_at': datetime(2026, 1, 1, tzinfo=UTC),
                'is_active': True,
            },
        )
        ids.append(row_id)
    await session.flush()
    return ids


async def _seed_action(session: AsyncSession) -> int:
    from src.inventory.actions.models import Action  # noqa: PLC0415

    result = await session.execute(sa.select(Action.id).where(Action.slug == 'read'))
    row = result.scalar_one_or_none()
    if row is not None:
        return row
    a = Action(slug='read', description='Read')
    session.add(a)
    await session.flush()
    return a.id


async def _seed_facts(
    session: AsyncSession,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
    base_action_id: int,
    count: int,
) -> list[uuid.UUID]:
    """Seed access_facts via raw SQL — ORM model deleted Phase 15 Step 16."""
    from sqlalchemy import select as _select  # noqa: PLC0415
    from src.inventory.resources.models import Resource  # noqa: PLC0415

    ids = []

    # Get the application_id from the resource to create sibling resources.
    res_result = await session.execute(_select(Resource).where(Resource.id == resource_id))
    base_res = res_result.scalar_one()

    for i in range(count):
        # Create a unique resource for each fact to avoid unique constraint.
        if i == 0:
            res_id = resource_id
        else:
            sibling = Resource(
                application_id=base_res.application_id,
                external_id=f'res-fact-{i}',
                kind='role',
                resource_type='role_assignment',
                resource_key=f'res-fact-key-{i}',
            )
            session.add(sibling)
            await session.flush()
            res_id = sibling.id

        fact_id = uuid.uuid4()
        await session.execute(
            sa.text(
                'INSERT INTO access_facts '
                '(id, subject_id, resource_id, action_id, effect, valid_from, observed_at, is_active) '
                'VALUES (:id, :subject_id, :resource_id, :action_id, :effect, :valid_from, :observed_at, :is_active)'
            ),
            {
                'id': fact_id,
                'subject_id': subject_id,
                'resource_id': res_id,
                'action_id': base_action_id,
                'effect': 'allow',
                'valid_from': datetime(2026, 1, 1, tzinfo=UTC),
                'observed_at': datetime(2026, 1, 1, tzinfo=UTC),
                'is_active': True,
            },
        )
        ids.append(fact_id)
    await session.flush()
    return ids


@pytest.mark.asyncio
async def test_migrate_access_artifacts_happy_path(db_session, session_factory, catalog, lake_session) -> None:
    """artifacts → status=completed, rows_read=rows_written equal count."""
    svc = _make_service()

    app_id = await _seed_application(db_session)
    await _seed_artifacts(db_session, app_id, count=100)
    await db_session.commit()

    async with session_factory() as s2:
        run = await svc.start_migration(
            s2,
            dataset=LakeMigrationDataset.access_artifacts,
            batch_size=50,
        )
        await s2.commit()

    async with session_factory() as s3:
        from src.engines.lake_migration.models import LakeMigrationRun  # noqa: PLC0415

        r = await s3.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run.id))
        run_r = r.scalar_one()
        await svc.migrate_access_artifacts(s3, run_r, lake_session=lake_session, catalog=catalog)
        await s3.commit()

    async with session_factory() as s4:
        from src.engines.lake_migration.models import LakeMigrationRun  # noqa: PLC0415

        r = await s4.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run.id))
        final_run = r.scalar_one()

    assert final_run.status == LakeMigrationStatus.completed
    assert final_run.rows_read == 100
    assert final_run.rows_written == 100


@pytest.mark.asyncio
async def test_migrate_access_artifacts_lake_batch_has_pg_migration_origin(
    db_session, session_factory, catalog, lake_session
) -> None:
    """The lake_batches row created by migration has origin='pg_migration'."""
    svc = _make_service()
    app_id = await _seed_application(db_session)
    await _seed_artifacts(db_session, app_id, count=5)
    await db_session.commit()

    async with session_factory() as s2:
        run = await svc.start_migration(s2, dataset=LakeMigrationDataset.access_artifacts, batch_size=50)
        await s2.commit()

    async with session_factory() as s3:
        from src.engines.lake_migration.models import LakeMigrationRun  # noqa: PLC0415

        r = await s3.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run.id))
        run_r = r.scalar_one()
        await svc.migrate_access_artifacts(s3, run_r, lake_session=lake_session, catalog=catalog)
        await s3.commit()

    async with session_factory() as s4:
        from src.inventory.lake_batches.models import LakeBatch  # noqa: PLC0415

        r = await s4.execute(sa.select(LakeBatch).where(LakeBatch.id == run.lake_batch_id))
        lb = r.scalar_one()
        assert lb.metadata_json is not None
        assert lb.metadata_json.get('origin') == 'pg_migration'


@pytest.mark.asyncio
async def test_migrate_access_facts_creates_delta_items(db_session, session_factory, catalog, lake_session) -> None:
    """facts → delta items with reason='pg_migration' and status='applied'."""
    svc = _make_service()

    app_id = await _seed_application(db_session)
    subj_id = await _seed_subject(db_session)
    res_id = await _seed_resource(db_session, app_id)
    action_id = await _seed_action(db_session)
    await _seed_facts(db_session, subj_id, res_id, action_id, count=20)
    await db_session.commit()

    from src.engines.reconciliation.models import ReconciliationDeltaItem  # noqa: PLC0415

    async with session_factory() as s2:
        run = await svc.start_migration(s2, dataset=LakeMigrationDataset.access_facts, batch_size=50)
        await s2.commit()

    async with session_factory() as s3:
        from src.engines.lake_migration.models import LakeMigrationRun  # noqa: PLC0415

        r = await s3.execute(sa.select(LakeMigrationRun).where(LakeMigrationRun.id == run.id))
        run_r = r.scalar_one()
        await svc.migrate_access_facts(s3, run_r, lake_session=lake_session, catalog=catalog)
        await s3.commit()

    async with session_factory() as s4:
        delta_result = await s4.execute(
            sa.select(ReconciliationDeltaItem).where(ReconciliationDeltaItem.reason == 'pg_migration')
        )
        items = delta_result.scalars().all()

    assert len(items) == 20
    assert all(i.status.value == 'applied' for i in items)
    assert all(i.operation.value == 'create' for i in items)
    assert all(i.source_artifact_id is None for i in items)
    assert all(len(i.natural_key_hash) == 64 for i in items)
