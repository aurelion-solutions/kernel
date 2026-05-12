"""Pipeline orchestrator persistence. Sole writer: platform/orchestrator/service.py.
Engines must not write to these tables (see ARCH_CONTEXT.md 'Pipeline state-ownership')."""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import datetime
import enum
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.db.base import Base

# ---------------------------------------------------------------------------
# Python enum types (StrEnum for ergonomic use in service.py and tests)
# ---------------------------------------------------------------------------


class PipelineRunStatus(str, enum.Enum):
    """Terminal and non-terminal states for a pipeline run.

    ``aborted`` is NOT a valid PipelineRun status — it is used only by StepRun
    (see StepRunStatus).  It is the forensic marker set by the reclaim
    transaction on the abandoned previous-attempt row.
    """

    pending = 'pending'
    running = 'running'
    awaiting_event = 'awaiting_event'
    cancelling = 'cancelling'
    completed = 'completed'
    failed = 'failed'
    failed_timeout = 'failed_timeout'
    cancelled = 'cancelled'


class StepRunStatus(str, enum.Enum):
    """Terminal and non-terminal states for a single step attempt.

    ``aborted`` is the terminal forensic status set by the reclaim transaction
    on the abandoned previous-attempt row (phase_18.md lines 107-113, 397-399).
    It is NOT used by PipelineRun.
    """

    pending = 'pending'
    running = 'running'
    awaiting_event = 'awaiting_event'
    completed = 'completed'
    failed = 'failed'
    failed_timeout = 'failed_timeout'
    # forensic marker for abandoned previous attempt; set by reclaim transaction
    aborted = 'aborted'
    cancelled = 'cancelled'


class PipelineEventWaiterStatus(str, enum.Enum):
    """States for an in-flight event waiter."""

    waiting = 'waiting'
    matched = 'matched'
    expired = 'expired'
    cancelled = 'cancelled'


class PipelineTriggerSource(str, enum.Enum):
    """Source that triggered a pipeline run.

    Note: 'http' covers manual POST /pipeline-runs triggers (no YAML trigger
    declaration needed).  Schedule and MQ triggers are declared in pipeline YAML.
    """

    http = 'http'
    mq = 'mq'
    schedule = 'schedule'
    retry = 'retry'


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class PipelineRun(Base):
    """Persistent state of a single pipeline execution.

    Only platform/orchestrator/service.py writes to this table.
    Engines MUST NOT write here directly (ARCH_CONTEXT pipeline state-ownership).
    """

    __tablename__ = 'pipeline_runs'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text('gen_random_uuid()'),
    )
    pipeline_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    pipeline_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    # sha256(canonical_json(args)) — computed by service.py; stored here for
    # the partial UNIQUE that enforces in-flight idempotency.
    content_hash: Mapped[str] = mapped_column(sa.CHAR(64), nullable=False)
    status: Mapped[PipelineRunStatus] = mapped_column(
        sa.Enum(PipelineRunStatus, name='pipeline_run_status', create_type=True),
        nullable=False,
        server_default='pending',
    )
    # step_name only; attempt is implicit via MAX(StepRun.attempt).
    current_step: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # Non-null on POST /retry rows; this is the partial-UNIQUE escape hatch.
    retry_of_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('pipeline_runs.id', ondelete='SET NULL', name='fk_pipeline_runs_retry_of_run_id'),
        nullable=True,
    )
    trigger_source: Mapped[PipelineTriggerSource] = mapped_column(
        sa.Enum(PipelineTriggerSource, name='pipeline_trigger_source', create_type=True),
        nullable=False,
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Format: <host>-<pid>-<slot>; cleared when run parks on awaiting_event.
    worker_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # DB-side work-claim heartbeat (3 s refresh, 10 s reclaim window).
    last_heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=text('now()'),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=text('now()'),
        onupdate=sa.func.now(),
    )

    # Relationships
    step_runs: Mapped[list[StepRun]] = relationship(
        'StepRun',
        back_populates='pipeline_run',
        cascade='all, delete-orphan',
        lazy='select',
    )
    retry_origin: Mapped[PipelineRun | None] = relationship(
        'PipelineRun',
        remote_side='PipelineRun.id',
        foreign_keys=[retry_of_run_id],
        lazy='select',
    )

    __table_args__ = (
        # Work-acquisition index: SELECT ... FOR UPDATE SKIP LOCKED on
        # (status='pending', stale-or-null heartbeat, ORDER BY created_at).
        sa.Index('ix_pipeline_runs_status_heartbeat_created', 'status', 'last_heartbeat_at', 'created_at'),
        # Partial UNIQUE: blocks duplicate in-flight runs for same
        # (pipeline_name, pipeline_version, content_hash) while letting
        # retries (retry_of_run_id IS NOT NULL) and terminal rows bypass.
        # AUTHORITATIVE DDL is in the migration; this declaration is
        # reflective only (ORM awareness).
        sa.Index(
            'uq_pipeline_runs_inflight_idempotency',
            'pipeline_name',
            'pipeline_version',
            'content_hash',
            unique=True,
            postgresql_where=text(
                "retry_of_run_id IS NULL AND status IN ('pending', 'running', 'awaiting_event', 'cancelling')"
            ),
        ),
    )


class StepRun(Base):
    """Persistent state of a single step attempt within a pipeline run.

    Only platform/orchestrator/service.py writes to this table.
    Engines MUST NOT write here directly (ARCH_CONTEXT pipeline state-ownership).
    """

    __tablename__ = 'step_runs'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text('gen_random_uuid()'),
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('pipeline_runs.id', ondelete='CASCADE', name='fk_step_runs_pipeline_run_id'),
        nullable=False,
    )
    step_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default='1')
    status: Mapped[StepRunStatus] = mapped_column(
        sa.Enum(StepRunStatus, name='step_run_status', create_type=True),
        nullable=False,
        server_default='pending',
    )
    args: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(astext_type=sa.Text()), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime.datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=text('now()'),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=text('now()'),
        onupdate=sa.func.now(),
    )

    # Relationships
    pipeline_run: Mapped[PipelineRun] = relationship(
        'PipelineRun',
        back_populates='step_runs',
        lazy='select',
    )
    event_waiter: Mapped[PipelineEventWaiter | None] = relationship(
        'PipelineEventWaiter',
        back_populates='step_run',
        cascade='all, delete-orphan',
        uselist=False,
        lazy='select',
    )

    __table_args__ = (
        # Composite unique: no two attempts with the same step_name within a run.
        sa.UniqueConstraint('pipeline_run_id', 'step_name', 'attempt', name='uq_step_runs_run_step_attempt'),
        # Supports run-detail queries.
        sa.Index('ix_step_runs_pipeline_run_id', 'pipeline_run_id'),
    )


class PipelineEventWaiter(Base):
    """Event waiter: one row per in-flight wait_for_event step attempt.

    A step_run_id has at most one active waiter at a time (UniqueConstraint).
    ``timeout`` is REQUIRED — no infinite waits are permitted.

    Only platform/orchestrator/service.py writes to this table.
    Engines MUST NOT write here directly (ARCH_CONTEXT pipeline state-ownership).
    """

    __tablename__ = 'pipeline_event_waiters'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text('gen_random_uuid()'),
    )
    step_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('step_runs.id', ondelete='CASCADE', name='fk_pipeline_event_waiters_step_run_id'),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    match: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    # expires_at is REQUIRED — the loader enforces 'timeout' as mandatory
    # on wait_for_event steps.
    expires_at: Mapped[datetime.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    status: Mapped[PipelineEventWaiterStatus] = mapped_column(
        sa.Enum(PipelineEventWaiterStatus, name='pipeline_event_waiter_status', create_type=True),
        nullable=False,
        server_default='waiting',
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=text('now()'),
    )

    # Relationships
    step_run: Mapped[StepRun] = relationship(
        'StepRun',
        back_populates='event_waiter',
        lazy='select',
    )

    __table_args__ = (
        # One waiter per step attempt.
        sa.UniqueConstraint('step_run_id', name='uq_pipeline_event_waiters_step_run_id'),
        # Matcher's per-event lookup.
        sa.Index('ix_pipeline_event_waiters_event_type', 'event_type'),
        # Beat's expiry sweep.
        sa.Index('ix_pipeline_event_waiters_expires_at', 'expires_at'),
    )
