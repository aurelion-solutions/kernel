# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""platform_executor_node — standalone pipeline executor process.

Entrypoint responsibilities (module-level, pre-import):
  1. load_dotenv()                 — populate os.environ from .env
  2. register_default_providers()  — wire platform secret providers into core factory

All other bootstrap (DB session, services, signal handling) lives inside
``_run()`` to keep the module testable.

Step 12a constraints:
  - Single slot (slot_index=0); multi-slot lands in Step 12b.
  - No HTTP server (/healthz, /readyz, /metrics) — Step 12b.
  - Hardcoded engine-action import list — replaced by auto-discovery in Step 14.

Graceful shutdown:
  SIGTERM/SIGINT → shutdown_event.set() → work_loop exits after current
  iteration completes.  If an action is running when the signal arrives it
  will run to completion before the process exits.  This is the documented
  Step 12a trade-off (Step 13 adds drain timeout + pipeline.step.aborted).
"""

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import signal

from dotenv import load_dotenv

load_dotenv()  # module-level: must precede any secrets/settings import
# ruff: noqa: E402
from src.platform.secrets.factory import register_default_providers  # noqa: E402

register_default_providers()  # module-level: must precede get_settings()

from src.core.config import get_settings  # noqa: E402
from src.core.db.session import get_session_factory  # noqa: E402
from src.core.mq.async_rpc_client import AsyncRabbitMQRPCClient  # noqa: E402
from src.platform.connectors.client import ConnectorClient  # noqa: E402
from src.platform.connectors.factory import set_process_connector_client  # noqa: E402
from src.platform.events.factory import event_sink_factory  # noqa: E402
from src.platform.events.service import EventService  # noqa: E402
from src.platform.logs.factory import log_sink_factory  # noqa: E402
from src.platform.logs.schemas import LogLevel  # noqa: E402
from src.platform.logs.service import LogService, merge_emit_component_trace_fields  # noqa: E402
from src.platform.orchestrator.liveness import heartbeat_publisher  # noqa: E402
from src.platform.orchestrator.loader import PipelineDefinition, PipelineDefinitionLoader  # noqa: E402
from src.platform.orchestrator.runner import WorkerIdentity, work_loop  # noqa: E402
from src.platform.orchestrator.service import _RECLAIM_STALE_THRESHOLD_SECONDS  # noqa: E402

_COMPONENT = 'pipeline_orchestrator.runner'
_PIPELINES_DIR = Path(__file__).parents[3] / 'pipelines'

# Bootstrap-tier default for the drain timeout.  Read from the environment so
# ops can tune it without modifying the binary.  Must be > the stale-reclaim
# threshold (10s) — see _RECLAIM_STALE_THRESHOLD_SECONDS in service.py.
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 60.0

# Bootstrap-tier config for heartbeat interval (>= 1.0s enforced by clamp).
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0
_MIN_HEARTBEAT_INTERVAL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Internal pipeline lookup (wraps the flat dict from load_dir)
# ---------------------------------------------------------------------------


class _PipelineLookup:
    """Thin wrapper around the loaded pipeline dict for the runner interface."""

    def __init__(self, pipelines: dict[str, PipelineDefinition]) -> None:
        self._pipelines = pipelines

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        defn = self._pipelines.get(name)
        if defn is None or defn.version != version:
            return None
        return defn


# ---------------------------------------------------------------------------
# Async run body
# ---------------------------------------------------------------------------


def _load_drain_timeout(log_service: LogService) -> float:
    """Read and clamp EXECUTOR_DRAIN_TIMEOUT_SECONDS from the environment.

    Clamps to ``max(value, _RECLAIM_STALE_THRESHOLD_SECONDS + 5)`` if the
    supplied value is too low to guarantee the drain window exceeds the stale
    threshold.  Emits a WARNING log when clamping.
    """
    raw = os.environ.get('EXECUTOR_DRAIN_TIMEOUT_SECONDS')
    try:
        value = float(raw) if raw is not None else _DEFAULT_DRAIN_TIMEOUT_SECONDS
    except (ValueError, TypeError):
        value = _DEFAULT_DRAIN_TIMEOUT_SECONDS

    min_safe = _RECLAIM_STALE_THRESHOLD_SECONDS + 5.0
    if value < min_safe:
        log_service.emit_safe(  # allowed-emit-safe: observability
            LogLevel.WARNING,
            f'EXECUTOR_DRAIN_TIMEOUT_SECONDS={value} is too low, clamped to {min_safe}s',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'configured_value': value, 'clamped_to': min_safe},
                component_id=_COMPONENT,
                target_id='runner',
            ),
        )
        return min_safe
    return value


def _load_heartbeat_interval(log_service: LogService) -> float:
    """Read and clamp EXECUTOR_HEARTBEAT_SECONDS from the environment.

    Clamps to ``max(value, _MIN_HEARTBEAT_INTERVAL_SECONDS)`` if the
    supplied value is too low.  Emits a WARNING log when clamping.
    Falls back to ``_DEFAULT_HEARTBEAT_INTERVAL_SECONDS`` on parse errors.
    """
    raw = os.environ.get('EXECUTOR_HEARTBEAT_SECONDS')
    try:
        value = float(raw) if raw is not None else _DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    except (ValueError, TypeError):
        value = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS

    if value < _MIN_HEARTBEAT_INTERVAL_SECONDS:
        log_service.emit_safe(  # allowed-emit-safe: observability
            LogLevel.WARNING,
            f'EXECUTOR_HEARTBEAT_SECONDS={value} too low, clamped to {_MIN_HEARTBEAT_INTERVAL_SECONDS}s',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'configured_value': value, 'clamped_to': _MIN_HEARTBEAT_INTERVAL_SECONDS},
                component_id=_COMPONENT,
                target_id='runner',
            ),
        )
        return _MIN_HEARTBEAT_INTERVAL_SECONDS
    return value


async def _run() -> None:
    # --- Service wiring -------------------------------------------------------
    log_sink = log_sink_factory.get(os.environ.get('AURELION_LOG_SINK_PROVIDER', 'file'))
    log_service = LogService(sink=log_sink)

    event_sink = event_sink_factory.get(os.environ.get('AURELION_EVENTS_PROVIDER', 'noop'))
    event_service = EventService(sink=event_sink)

    # --- Bootstrap-tier config ------------------------------------------------
    drain_timeout = _load_drain_timeout(log_service)
    heartbeat_interval = _load_heartbeat_interval(log_service)

    # --- Engine-action imports (hardcoded for Step 12a) ----------------------
    # Each import triggers the @register_action decorators in that module, which
    # populates ACTION_REGISTRY.  Auto-discovery replaces this block in Step 14.
    import src.engines.access_analysis.assessment_preview.actions as _aaap_actions  # noqa: F401, PLC0415
    import src.engines.access_analysis.capability_preview.actions as _aacp_actions  # noqa: F401, PLC0415
    import src.engines.access_analysis.reports.actions as _aar_actions  # noqa: F401, PLC0415
    import src.engines.effective_access.actions as _ea_actions  # noqa: F401, PLC0415
    import src.engines.policy_assessment.policy_types.sod.actions as _sod_actions  # noqa: F401, PLC0415
    import src.engines.provisioning.actions as _prov_actions  # noqa: F401, PLC0415
    import src.engines.reconciliation.actions as _recon_actions  # noqa: F401, PLC0415
    import src.engines.sync_apply.actions as _sa_actions  # noqa: F401, PLC0415

    # --- Connector RPC client (eager init for connector-backed actions) --------
    # Mirrors platform_api/main.py:73-78.  Opened once at process start so that
    # connector-backed action handlers do not pay a connection handshake per call.
    _settings = get_settings()
    _mq = _settings.rabbitmq
    rpc_client = AsyncRabbitMQRPCClient(
        url=_mq.url,
        commands_exchange=_mq.connector_commands_exchange,
        responses_exchange=_mq.connector_responses_exchange,
    )
    await rpc_client.connect()
    set_process_connector_client(ConnectorClient(rpc_client=rpc_client))

    # --- Pipeline definitions -------------------------------------------------
    loader = PipelineDefinitionLoader()
    pipelines = loader.load_dir(_PIPELINES_DIR)
    pipeline_lookup = _PipelineLookup(pipelines)

    # --- Shutdown handling ---------------------------------------------------
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # --- Start ---------------------------------------------------------------
    started_at = datetime.now(UTC)
    worker = WorkerIdentity.create(slot_index=0)
    log_service.emit_safe(  # allowed-emit-safe: observability
        LogLevel.INFO,
        'platform_executor_node started',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'slot_index': 0, 'worker_id': worker.worker_id},
            component_id=_COMPONENT,
            target_id='runner',
        ),
    )

    heartbeat_stop_event = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        heartbeat_publisher(
            events=event_service,
            logs=log_service,
            worker=worker,
            started_at=started_at,
            pipelines_loaded=len(pipelines),
            interval=heartbeat_interval,
            stop_event=heartbeat_stop_event,
        )
    )

    try:
        await work_loop(
            session_factory=get_session_factory(),
            pipeline_loader=pipeline_lookup,
            events=event_service,
            logs=log_service,
            slot_index=0,
            shutdown_event=shutdown_event,
            drain_timeout=drain_timeout,
        )
    finally:
        heartbeat_stop_event.set()
        await heartbeat_task

    log_service.emit_safe(  # allowed-emit-safe: observability
        LogLevel.INFO,
        'platform_executor_node stopped',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'slot_index': 0, 'worker_id': worker.worker_id},
            component_id=_COMPONENT,
            target_id='runner',
        ),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Synchronous entry point — called by ``python -m src.runtimes.platform_executor_node.main``."""
    asyncio.run(_run())


if __name__ == '__main__':
    main()
