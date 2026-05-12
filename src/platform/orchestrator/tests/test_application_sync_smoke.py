# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke test for application_sync pipeline (Phase 18 Step 21).

Two test classes:

  - TestApplicationSyncLoad  — loader probe: YAML parses and validates.
  - TestApplicationSyncSmoke — full end-to-end drive through matcher + runner
    using real PostgreSQL + real in-process Iceberg (SQLite catalog).

Architecture notes
------------------
* Real engine actions stay registered (no ACTION_REGISTRY._clear_for_tests()).
  Action modules are imported at file top to force registration.
* Process lake deps are set for the duration of the smoke test and torn down
  via try/finally so they do not leak into unrelated tests.
* Lake setup (<40 lines) is inlined here — cross-slice conftest imports are
  forbidden (TASK.md B2).
* PG shim ``access_facts`` is seeded with one row so that
  ``effective_access.project_application`` can produce ≥1 ``EffectiveGrant``.
  This is required because ``project_application`` reads from the PG shim, not
  from Iceberg (see effective_access/repository.py:fetch_application_facts_with_initiatives).
* ``now`` is included in the synthetic MQ payload; the real
  ``connector.result.received`` emitter (engines/ingest/service.py) does not
  include ``now`` — a follow-up step must extend the payload.
* LakeSessionFactory is configured with a sync pg_dsn so that Pattern-3
  ``ref_actions_local`` TEMP TABLE is bootstrapped automatically on each
  DuckDB connection (required by reconciliation/_phase_resolve_action_ids).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse
import uuid

import pyarrow as pa
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.engines.reconciliation.contracts import Handler, NormalizationResult
from src.engines.reconciliation.registry import _reset_registry_for_tests, register_handler
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.lake.factory import set_process_lake_deps
from src.platform.lake.provisioning import ensure_tables
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.loader import PipelineDefinition, PipelineDefinitionLoader
from src.platform.orchestrator.matcher import matcher_tick
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
    StepRun,
)
from src.platform.orchestrator.runner import WorkerIdentity, run_one_iteration
from src.platform.orchestrator.service import PipelineOrchestratorService

# isort: split
# Force action registration — side-effect imports must follow other src imports.
import src.engines.effective_access.actions  # noqa: F401, E402
import src.engines.reconciliation.actions  # noqa: F401, E402
import src.engines.sync_apply.actions  # noqa: F401, E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PIPELINES_DIR = Path(__file__).parents[4] / 'pipelines'
_YAML_PATH = _PIPELINES_DIR / 'application_sync.yaml'


# ---------------------------------------------------------------------------
# Class 1 — Loader probe
# ---------------------------------------------------------------------------


class TestApplicationSyncLoad:
    def test_application_sync_yaml_loads(self) -> None:
        """YAML must parse without error using the real loader + registered actions.

        Actions may have been cleared by other tests that call
        ACTION_REGISTRY._clear_for_tests().  Clear the registry first, then
        reload action modules to re-execute @register_action decorators.
        """
        import importlib  # noqa: PLC0415

        import src.engines.effective_access.actions as _ea  # noqa: PLC0415
        import src.engines.reconciliation.actions as _ra  # noqa: PLC0415
        import src.engines.sync_apply.actions as _sa  # noqa: PLC0415
        from src.platform.orchestrator.registry import ACTION_REGISTRY  # noqa: PLC0415

        ACTION_REGISTRY._clear_for_tests()
        importlib.reload(_ea)
        importlib.reload(_ra)
        importlib.reload(_sa)

        loader = PipelineDefinitionLoader()
        defn = loader.load_file(_YAML_PATH)
        assert defn.name == 'application_sync'
        assert defn.version == 1
        assert defn.schema_version == 1
        assert len(defn.steps) == 6


# ---------------------------------------------------------------------------
# Helpers — dict-backed loader (mirrors test_runner_integration.py)
# ---------------------------------------------------------------------------


class _DictLoader:
    def __init__(self, mapping: dict[tuple[str, int], PipelineDefinition]) -> None:
        self._mapping = mapping

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        return self._mapping.get((name, version))


def _make_worker() -> WorkerIdentity:
    return WorkerIdentity(worker_id='smoke-host-1-0', hostname='smoke-host', pid=1, slot_index=0)


def _service_factory(
    capturing: CapturingEventService,
) -> Callable[[AsyncSession], PipelineOrchestratorService]:
    def factory(session: AsyncSession) -> PipelineOrchestratorService:
        return PipelineOrchestratorService(
            session=session,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )

    return factory


# ---------------------------------------------------------------------------
# Inline lake setup — <40 lines, no cross-slice conftest imports
# ---------------------------------------------------------------------------


def _make_sync_pg_dsn() -> str:
    """Convert the asyncpg TEST_DATABASE_URL to a libpq-compatible DSN.

    DuckDB Pattern-3 (ATTACH + ref_actions_local) requires a sync libpq DSN,
    not an asyncpg one.  We strip the '+asyncpg' driver qualifier.
    """
    from src.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    parsed = urlparse(TEST_DATABASE_URL)
    sync_scheme = parsed.scheme.replace('+asyncpg', '')
    return urlunparse(parsed._replace(scheme=sync_scheme))


def _setup_lake(tmp_path: Path) -> tuple[LakeSettings, LakeSessionFactory, Any]:
    """Provision SQLite-backed Iceberg lake; return (settings, factory, catalog).

    Uses ensure_tables to provision normalized.access_facts (production schema).
    raw.access_artifacts is re-created with required=False on all fields so that
    PyArrow nullable arrays can be appended without schema-compatibility errors
    (mirrors test_pipeline_delta.py pattern).
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec  # noqa: PLC0415
    from pyiceberg.schema import Schema  # noqa: PLC0415
    from pyiceberg.transforms import IdentityTransform  # noqa: PLC0415
    from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType  # noqa: PLC0415

    settings = LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/smoke_catalog.db',
        warehouse_uri=f'file://{tmp_path}/smoke_warehouse',
        storage_provider='file',
        artifacts_write_backend='iceberg',
    )
    reset_catalog_cache_for_tests()
    catalog = get_catalog(settings, log_service=NoOpLogService())
    ensure_tables(catalog, log_service=NoOpLogService())

    # Re-create raw.access_artifacts with required=False to accept nullable PyArrow arrays.
    try:
        catalog.drop_table(('raw', 'access_artifacts'))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass
    art_schema = Schema(
        NestedField(1, 'id', StringType(), required=False),
        NestedField(2, 'application_id', StringType(), required=False),
        NestedField(3, 'artifact_type', StringType(), required=False),
        NestedField(4, 'external_id', StringType(), required=False),
        NestedField(5, 'payload', StringType(), required=False),
        NestedField(6, 'raw_name', StringType(), required=False),
        NestedField(7, 'effect', StringType(), required=False),
        NestedField(8, 'valid_from', TimestamptzType(), required=False),
        NestedField(9, 'valid_until', TimestamptzType(), required=False),
        NestedField(10, 'is_active', BooleanType(), required=False),
        NestedField(11, 'tombstoned_at', TimestamptzType(), required=False),
        NestedField(12, 'observed_at', TimestamptzType(), required=False),
        NestedField(13, 'ingested_at', TimestamptzType(), required=False),
        NestedField(14, 'ingest_batch_id', StringType(), required=False),
    )
    art_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name='artifact_type')
    )
    catalog.create_table(('raw', 'access_artifacts'), schema=art_schema, partition_spec=art_spec)

    pg_dsn = _make_sync_pg_dsn()
    lake_factory = LakeSessionFactory(settings=settings, log_service=NoOpLogService(), pg_dsn=pg_dsn)
    return settings, lake_factory, catalog


def _seed_artifacts(catalog: Any, app_id: uuid.UUID, now: datetime) -> None:
    """Append one role artifact row to raw.access_artifacts so reconcile has a delta."""
    import json  # noqa: PLC0415

    payload_json = json.dumps(
        {
            'subject_id': str(uuid.uuid4()),
            'resource_key': 'smoke-res-key',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }
    )
    arrow_table = pa.table(
        {
            'id': pa.array([str(uuid.uuid4())], type=pa.string()),
            'application_id': pa.array([str(app_id)], type=pa.string()),
            'artifact_type': pa.array(['role'], type=pa.string()),
            'external_id': pa.array(['smoke-ext-0'], type=pa.string()),
            'payload': pa.array([payload_json], type=pa.string()),
            'raw_name': pa.array(['smoke-role-0'], type=pa.string()),
            'effect': pa.array(['allow'], type=pa.string()),
            'valid_from': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'valid_until': pa.array([None], type=pa.timestamp('us', tz='UTC')),
            'is_active': pa.array([True], type=pa.bool_()),
            'tombstoned_at': pa.array([None], type=pa.timestamp('us', tz='UTC')),
            'observed_at': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'ingested_at': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'ingest_batch_id': pa.array([None], type=pa.string()),
        }
    )
    tbl = catalog.load_table(('raw', 'access_artifacts'))
    tbl.append(arrow_table)


# ---------------------------------------------------------------------------
# Passthrough reconciliation handler (mirrors test_pipeline_delta.py)
# ---------------------------------------------------------------------------


class _RoleHandler(Handler):
    """Minimal reconciliation handler for 'role' artifact_type.

    Parses the JSON payload that _seed_artifacts writes and resolves (or creates)
    the Resource in PG.  Returns one NormalizationResult per artifact.
    """

    async def handle(self, artifact: Any, session: Any) -> list[NormalizationResult]:
        import json

        raw = artifact.payload
        if isinstance(raw, dict):
            payload = raw
        elif raw:
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return []
        else:
            payload = {}

        subject_id_str = payload.get('subject_id')
        resource_key = payload.get('resource_key')
        action_slug = payload.get('action_slug', 'read')
        effect = payload.get('effect', 'allow')

        if not subject_id_str or not resource_key:
            return []

        from sqlalchemy import select
        from src.inventory.resources.models import Resource

        result = await session.execute(select(Resource).where(Resource.resource_key == resource_key))
        resource = result.scalar_one_or_none()
        if resource is None:
            resource = Resource(
                external_id=resource_key,
                application_id=artifact.application_id,
                kind='database',
                resource_type='database',
                resource_key=resource_key,
            )
            session.add(resource)
            await session.flush()

        return [
            NormalizationResult(
                subject_id=uuid.UUID(subject_id_str),
                account_id=None,
                resource_id=resource.id,
                action_slug=action_slug,
                effect=effect,
                valid_from=None,
                valid_until=None,
            )
        ]


# ---------------------------------------------------------------------------
# Class 2 — Full smoke test
# ---------------------------------------------------------------------------


class TestApplicationSyncSmoke:
    async def test_application_sync_smoke_runs_to_completion(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """End-to-end: matcher_tick → 6-step pipeline → completed.

        Asserts:
          - pipeline_run.status == completed, 6 step_runs all completed.
          - ≥1 effective_grant for the application.
          - Events: pipeline.run.started, ×6 pipeline.step.completed,
            pipeline.run.completed, ≥1 inventory.access_fact.*.
        """
        # Ensure ACTION_REGISTRY has real engine actions.  Other tests may have called
        # ACTION_REGISTRY._clear_for_tests() and left it empty.  We clear first so that
        # importlib.reload does not hit DuplicateActionError.
        import importlib  # noqa: PLC0415

        import src.engines.effective_access.actions as _ea  # noqa: PLC0415
        import src.engines.reconciliation.actions as _ra  # noqa: PLC0415
        import src.engines.sync_apply.actions as _sa  # noqa: PLC0415
        from src.platform.orchestrator.registry import ACTION_REGISTRY  # noqa: PLC0415

        ACTION_REGISTRY._clear_for_tests()
        importlib.reload(_ea)
        importlib.reload(_ra)
        importlib.reload(_sa)

        _reset_registry_for_tests()
        register_handler('role', _RoleHandler())

        # --- Lake setup ---
        settings, lake_factory, catalog = _setup_lake(tmp_path)

        try:
            set_process_lake_deps(
                catalog=catalog,
                session_factory=lake_factory,
                settings=settings,
            )

            now = datetime.now(UTC)

            # --- Seed PG ---
            async with session_factory() as session:
                from src.inventory.actions.models import Action as RefAction  # noqa: PLC0415
                from src.inventory.employees.models import Employee  # noqa: PLC0415
                from src.inventory.persons.models import Person  # noqa: PLC0415
                from src.inventory.resources.models import Resource  # noqa: PLC0415
                from src.inventory.subjects.models import Subject, SubjectKind  # noqa: PLC0415
                from src.platform.applications.models import Application  # noqa: PLC0415

                app = Application(
                    name=f'smoke-app-{uuid.uuid4()}',
                    code=f'smk-{uuid.uuid4().hex[:8]}',
                    config={},
                    required_connector_tags=[],
                    is_active=True,
                )
                session.add(app)
                await session.flush()
                app_id = app.id

                # Resource required by access_facts shim FK + reconciliation handler
                resource = Resource(
                    external_id='smoke-res-key',
                    application_id=app_id,
                    kind='database',
                    resource_type='database',
                    resource_key='smoke-res-key',
                )
                session.add(resource)
                await session.flush()
                res_id = resource.id

                # Look up action_id for 'read' (seeded by conftest)
                action_row = (await session.execute(sa.select(RefAction).where(RefAction.slug == 'read'))).scalar_one()
                act_id = action_row.id

                # Build minimal Person → Employee → Subject chain for access_facts FK.
                # Subject.ck_subjects_principal_exactly_one requires principal_employee_id
                # for kind='employee', which requires a real Employee row, which requires Person.
                person = Person(external_id=f'smoke-person-{uuid.uuid4()}', full_name='Smoke Subject')
                session.add(person)
                await session.flush()

                employee = Employee(person_id=person.id, is_locked=False)
                session.add(employee)
                await session.flush()

                subject = Subject(
                    external_id=f'smoke-subj-{uuid.uuid4()}',
                    kind=SubjectKind.employee,
                    status='active',
                    principal_employee_id=employee.id,
                )
                session.add(subject)
                await session.flush()
                subj_id = subject.id

                # Seed one access_facts shim row.
                # access_facts.subject_id FK references subjects (which now exists).
                # session_replication_role='replica' bypasses the FK on access_facts
                # that points to subjects (we have a real subject but the shim DDL
                # may have additional FK guards we want to avoid).
                af_id = uuid.uuid4()
                await session.execute(sa.text("SET session_replication_role = 'replica'"))
                await session.execute(
                    sa.text(
                        'INSERT INTO access_facts '
                        '(id, subject_id, resource_id, action_id, effect,'
                        ' valid_from, is_active, observed_at, created_at)'
                        ' VALUES (:id, :subj, :res, :act, :eff, :vf, TRUE, :obs, :cr)'
                    ),
                    {
                        'id': af_id,
                        'subj': subj_id,
                        'res': res_id,
                        'act': act_id,
                        'eff': 'allow',
                        'vf': now,
                        'obs': now,
                        'cr': now,
                    },
                )
                await session.execute(sa.text("SET session_replication_role = 'origin'"))

                # Initiative links the access_fact to a reason for existence.
                # project_application only creates effective_grants for (fact, initiative) pairs.
                from src.inventory.initiatives.models import Initiative, InitiativeType  # noqa: PLC0415

                initiative = Initiative(
                    access_fact_id=af_id,
                    type=InitiativeType.birthright,
                    origin='smoke-test',
                    valid_from=now,
                )
                session.add(initiative)
                await session.commit()

            # --- Seed Iceberg artifacts ---
            _seed_artifacts(catalog, app_id, now)

            # --- Load pipeline ---
            loader_obj = PipelineDefinitionLoader()
            defn = loader_obj.load_file(_YAML_PATH)
            dict_loader = _DictLoader({('application_sync', 1): defn})

            capturing = CapturingEventService()

            # Patch _build_event_service in sync_apply.actions so that inventory.access_fact.*
            # events from SyncApplyService flow into `capturing` rather than a noop sink.
            # AURELION_EVENTS_PROVIDER=noop (set by conftest session fixture) would otherwise
            # swallow all events emitted by engine actions that build their own EventService.
            from unittest.mock import patch  # noqa: PLC0415

            sync_apply_event_service = EventService(sink=capturing)
            reconcile_event_service = EventService(sink=capturing)

            with (
                patch(
                    'src.engines.sync_apply.actions._build_event_service',
                    return_value=sync_apply_event_service,
                ),
                patch(
                    'src.engines.reconciliation.actions._build_event_service',
                    return_value=reconcile_event_service,
                ),
            ):
                # --- Trigger via matcher_tick ---
                await matcher_tick(
                    event_type='connector.result.received',
                    routing_key='connector.result.received',
                    payload={
                        'application_id': str(app_id),
                        'task_id': str(uuid.uuid4()),
                        'result_id': str(uuid.uuid4()),
                        'now': now.isoformat(),
                    },
                    correlation_id='smoke-corr-1',
                    causation_id=None,
                    session_factory=session_factory,
                    defs_provider=lambda: {'application_sync': defn},
                    service_factory=_service_factory(capturing),
                    log_service=NoOpLogService(),
                )

                # Assert one pending pipeline_run created
                async with session_factory() as session:
                    runs = (
                        (
                            await session.execute(
                                sa.select(PipelineRun).where(
                                    PipelineRun.pipeline_name == 'application_sync',
                                    PipelineRun.trigger_source == PipelineTriggerSource.mq,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                assert len(runs) == 1, f'Expected 1 pipeline_run, got {len(runs)}'
                assert runs[0].status == PipelineRunStatus.pending
                run_id = runs[0].id

                # --- Drive loop ---
                outcome = 'pending'
                for _ in range(20):
                    outcome = await run_one_iteration(
                        session_factory,
                        worker=_make_worker(),
                        pipeline_loader=dict_loader,
                        events=EventService(sink=capturing),
                        logs=NoOpLogService(),
                    )
                    if outcome in {'completed', 'failed', 'awaiting_event'}:
                        break

            # Collect step_run details for failure diagnosis
            async with session_factory() as diag_session:
                diag_step_runs = (
                    (await diag_session.execute(sa.select(StepRun).where(StepRun.pipeline_run_id == run_id)))
                    .scalars()
                    .all()
                )
            diag_info = [(sr.step_name, sr.status, sr.error) for sr in diag_step_runs]
            assert outcome == 'completed', f'Pipeline outcome: {outcome!r}; steps: {diag_info}'

            # --- Assert pipeline_run state ---
            async with session_factory() as session:
                final_run = (
                    await session.execute(
                        sa.select(PipelineRun).where(PipelineRun.id == run_id).execution_options(populate_existing=True)
                    )
                ).scalar_one()
                step_runs = (
                    (await session.execute(sa.select(StepRun).where(StepRun.pipeline_run_id == run_id))).scalars().all()
                )

            assert final_run.status == PipelineRunStatus.completed
            assert final_run.finished_at is not None
            assert len(step_runs) == 6, f'Expected 6 step_runs, got {len(step_runs)}'
            for sr in step_runs:
                assert sr.status.value == 'completed', f'Step {sr.step_name!r} status: {sr.status!r}'

            # --- Assert effective_grants ---
            from src.engines.effective_access.models import EffectiveGrant  # noqa: PLC0415

            async with session_factory() as session:
                grant_count = (
                    await session.execute(
                        sa.select(sa.func.count(EffectiveGrant.id)).where(EffectiveGrant.application_id == app_id)
                    )
                ).scalar_one()
            assert grant_count >= 1, f'Expected ≥1 effective_grant, got {grant_count}'

            # --- Assert events ---
            event_types = [e.event_type for e in capturing.emitted]
            assert 'pipeline.run.started' in event_types
            assert event_types.count('pipeline.step.completed') == 6
            assert 'pipeline.run.completed' in event_types
            access_fact_events = [e for e in capturing.emitted if e.event_type.startswith('inventory.access_fact.')]
            assert len(access_fact_events) >= 1, 'Expected ≥1 inventory.access_fact.* event from sync_apply'

        finally:
            _reset_registry_for_tests()
            reset_catalog_cache_for_tests()
            # Clear process lake deps to avoid leaking state to other tests
            from src.platform.lake.factory import reset_process_lake_deps_for_tests  # noqa: PLC0415

            reset_process_lake_deps_for_tests()
