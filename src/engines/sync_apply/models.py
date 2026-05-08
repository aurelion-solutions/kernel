# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SyncApplyRun + SyncApplyResult ORM models.

Three PG enum types are OWNED by this slice (create_type=True):
  - ``sync_apply_run_status``
  - ``sync_apply_run_mode``
  - ``sync_apply_result_status``

Downstream consumers reuse them via ``Enum(..., create_type=False)``.
Do NOT re-declare these enums elsewhere.

Soft lake references
--------------------
``SyncApplyResult.fact_id`` is a plain UUID column with NO database-level
foreign key.  It points at a ``normalized.access_facts`` Iceberg row whose
identity is maintained by service-level validation, not DB constraints.
This mirrors the pattern used in ``ReconciliationDeltaItem.existing_fact_id``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base

# ---------------------------------------------------------------------------
# Python StrEnum definitions
# ---------------------------------------------------------------------------


class SyncApplyRunStatus(StrEnum):
    running = 'running'
    completed = 'completed'
    failed = 'failed'
    partially_applied = 'partially_applied'


class SyncApplyRunMode(StrEnum):
    auto_apply = 'auto_apply'
    manual_apply = 'manual_apply'
    selected_items = 'selected_items'
    dry_run = 'dry_run'


class SyncApplyResultStatus(StrEnum):
    applied = 'applied'
    failed = 'failed'
    skipped = 'skipped'


# ---------------------------------------------------------------------------
# SQLAlchemy Enum column types — owned here, create_type=True
# ---------------------------------------------------------------------------

_run_status_enum = SaEnum(
    SyncApplyRunStatus,
    name='sync_apply_run_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_run_mode_enum = SaEnum(
    SyncApplyRunMode,
    name='sync_apply_run_mode',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_result_status_enum = SaEnum(
    SyncApplyResultStatus,
    name='sync_apply_result_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class SyncApplyRun(Base):
    """One row per sync/apply execution against a reconciliation run.

    Statuses:
    - ``running``           — apply in progress.
    - ``completed``         — all items applied successfully.
    - ``failed``            — apply terminated with a fatal error.
    - ``partially_applied`` — some items applied; remainder failed.

    Modes:
    - ``auto_apply``      — all approved delta items applied automatically.
    - ``manual_apply``    — operator-confirmed bulk apply.
    - ``selected_items``  — specific item IDs passed in the request.
    - ``dry_run``         — simulate only; no Iceberg writes.

    No relationship() declarations — cross-slice joins are explicit.
    """

    __tablename__ = 'sync_apply_runs'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('reconciliation_runs.id', ondelete='RESTRICT'),
        nullable=False,
    )
    status: Mapped[SyncApplyRunStatus] = mapped_column(
        _run_status_enum,
        nullable=False,
        default=SyncApplyRunStatus.running,
        server_default='running',
    )
    mode: Mapped[SyncApplyRunMode] = mapped_column(
        _run_mode_enum,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    requested_by: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
        comment='Free-form actor id; set by service layer.',
    )
    applied_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    failed_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    error: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )

    __table_args__ = (
        sa.Index('ix_sync_apply_runs_reconciliation_run_id', 'reconciliation_run_id'),
        sa.Index('ix_sync_apply_runs_status', 'status'),
    )


class SyncApplyResult(Base):
    """One row per delta item processed by a sync/apply run.

    The result links the apply run to the specific delta item that was
    processed, and optionally records the lake-side fact UUID and Iceberg
    snapshot id produced by the write.

    Soft lake reference (NO DB FK):
    - ``fact_id`` → ``normalized.access_facts`` Iceberg row

    No relationship() declarations — cross-slice joins are explicit.
    """

    __tablename__ = 'sync_apply_results'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    sync_apply_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('sync_apply_runs.id', ondelete='CASCADE'),
        nullable=False,
    )
    delta_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('reconciliation_delta_items.id', ondelete='RESTRICT'),
        nullable=False,
    )
    status: Mapped[SyncApplyResultStatus] = mapped_column(
        _result_status_enum,
        nullable=False,
    )
    # Soft lake reference — intentionally no ForeignKey()
    fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment='Points at normalized.access_facts Iceberg row; no DB FK.',
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        nullable=True,
        comment='Iceberg snapshot id from lake_writer commit.',
    )
    error: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Composite index: supports per-run result pagination and crash-recovery
        # scans in Step 11/12.
        sa.Index(
            'ix_sync_apply_results_run_status',
            'sync_apply_run_id',
            'status',
        ),
        # Single-column index on delta_item_id for crash-recovery scan in Step 11:
        # "find all results for a given delta_item_id to check if already written".
        sa.Index('ix_sync_apply_results_delta_item_id', 'delta_item_id'),
    )
