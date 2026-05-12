# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the inventory.initiatives slice (Phase 19 Step E4).

Registered actions:
  - (inventory.initiatives, scan_for_replan) — stateless scanner.
    Queries initiatives whose valid_from or valid_until falls within a
    sliding window [now() - lookback, now() + 60s].  For each initiative
    with a known subject_ref, emits subject.replan.required with
    idempotency_key = sha1(subject_ref + str(window_bucket)) so that
    overlapping scanner runs produce the same key and the matcher (E3)
    collapses duplicates.

    window_bucket = floor(unix_timestamp / 60) — stable for 1 minute.

Library-module discipline: no get_settings(), no load_dotenv(),
no register_default_providers() at import time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import uuid

from pydantic import BaseModel, ConfigDict
from src.inventory.initiatives.repository import scan_for_replan_window
from src.platform.orchestrator.registry import ActionContext, register_action

_COMPONENT = 'inventory.initiatives'
_LOOKAHEAD_SECONDS = 60  # scanner always looks 1 minute ahead


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ScanForReplanArgs(BaseModel):
    """Args for inventory.initiatives.scan_for_replan action (no args required)."""

    model_config = ConfigDict(extra='forbid')


class ScanForReplanResult(BaseModel):
    """Result envelope for inventory.initiatives.scan_for_replan action."""

    model_config = ConfigDict(extra='forbid')

    subjects_queued: int
    initiatives_scanned: int


# ---------------------------------------------------------------------------
# Core scan logic (extracted for testability)
# ---------------------------------------------------------------------------


def _window_bucket(now: datetime) -> int:
    """Return floor(unix_epoch_seconds / 60) — stable for one full minute."""
    return int(now.timestamp()) // 60


def _build_idempotency_key(subject_ref: str, bucket: int) -> str:
    """SHA-1 of 'subject_ref:bucket' — short and stable within a window."""
    raw = f'{subject_ref}:{bucket}'
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()


async def run_scan_for_replan(
    ctx: ActionContext,
    *,
    now: datetime | None = None,
    lookback_seconds: int | None = None,
) -> ScanForReplanResult:
    """Scan initiatives in the sliding window and emit replan events.

    Parameters
    ----------
    ctx:
        ActionContext from the orchestrator runner.
    now:
        Override for current time (default: datetime.now(UTC)).  Used in tests.
    lookback_seconds:
        Override for look-back window in seconds (default: RuntimeSettingsConfig.scanner_window_lookback_seconds).
    """
    from src.platform.events.schemas import EventEnvelope, EventParticipantKind
    from src.platform.events.service import EventService, noop_event_service
    from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

    effective_now = now if now is not None else datetime.now(UTC)

    if lookback_seconds is None:
        settings = RuntimeSettingsConfig()
        lookback_seconds = settings.scanner_window_lookback_seconds

    window_start = effective_now - timedelta(seconds=lookback_seconds)
    window_end = effective_now + timedelta(seconds=_LOOKAHEAD_SECONDS)

    initiatives = await scan_for_replan_window(
        ctx.session,
        window_start=window_start,
        window_end=window_end,
    )

    if not initiatives:
        return ScanForReplanResult(subjects_queued=0, initiatives_scanned=0)

    # Build event service from ctx (action context carries a log_service but not
    # an event_service directly — we use noop_event_service as a default and
    # rely on the platform to wire a real sink in production).
    # The event_service is injected via ctx if present (test DI point).
    event_service: EventService
    if hasattr(ctx, 'event_service') and ctx.event_service is not None:  # type: ignore[union-attr]
        event_service = ctx.event_service  # type: ignore[union-attr]
    else:
        event_service = noop_event_service

    bucket = _window_bucket(effective_now)

    # Track unique (subject_ref, subject_type) pairs to avoid emitting the
    # same subject twice when multiple initiatives share a subject.
    seen_subjects: set[str] = set()
    subjects_queued = 0

    for initiative in initiatives:
        subject_ref = initiative.subject_ref
        if subject_ref is None:
            continue  # defensive — scan_for_replan_window already filters NULLs

        subject_type = initiative.subject_type or 'employee'

        if subject_ref in seen_subjects:
            continue
        seen_subjects.add(subject_ref)

        idempotency_key = _build_idempotency_key(subject_ref, bucket)

        await event_service.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='subject.replan.required',
                occurred_at=effective_now,
                correlation_id=idempotency_key,
                causation_id=None,
                payload={
                    'subject_id': subject_ref,
                    'subject_type': subject_type,
                    'idempotency_key': idempotency_key,
                    'window_bucket': bucket,
                    'scanner': _COMPONENT,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=subject_ref,
            )
        )
        subjects_queued += 1

    return ScanForReplanResult(
        subjects_queued=subjects_queued,
        initiatives_scanned=len(initiatives),
    )


# ---------------------------------------------------------------------------
# Registered action handler
# ---------------------------------------------------------------------------


@register_action(  # type: ignore[arg-type]
    engine='inventory.initiatives',
    action='scan_for_replan',
    args_schema=ScanForReplanArgs,
    result_schema=ScanForReplanResult,
    idempotent=True,
)
async def scan_for_replan_action(
    args: ScanForReplanArgs,  # noqa: ARG001
    ctx: ActionContext,
) -> ScanForReplanResult:
    """Action handler: scan initiatives for scheduled replan triggers.

    Stateless — safe to run multiple times within the same minute.
    The emitted events carry an idempotency_key tied to window_bucket so
    overlapping pipeline runs produce the same key and the matcher (E3)
    collapses duplicates via unique constraint on (pipeline_name, idempotency_key).
    """
    from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

    settings = RuntimeSettingsConfig()
    return await run_scan_for_replan(
        ctx,
        lookback_seconds=settings.scanner_window_lookback_seconds,
    )


def _ensure_scan_for_replan_registered() -> None:
    """Re-register scan_for_replan if it was removed by _clear_for_tests.

    Called by test modules that need to verify action registration after
    another test's _registry_isolation fixture cleared the ACTION_REGISTRY.
    Safe to call multiple times (no-op if already registered).
    This is a test-only helper exposed at module level.
    """
    from src.platform.orchestrator.registry import ACTION_REGISTRY  # noqa: PLC0415

    ACTION_REGISTRY._register_if_absent(  # type: ignore[attr-defined]
        engine='inventory.initiatives',
        action='scan_for_replan',
        args_schema=ScanForReplanArgs,
        result_schema=ScanForReplanResult,
        idempotent=True,
        handler=scan_for_replan_action,  # type: ignore[arg-type]
    )
