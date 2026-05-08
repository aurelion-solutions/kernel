# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ReconciliationRun + ReconciliationDeltaItem ORM models.

Three PG enum types are OWNED by this slice (create_type=True):
  - ``reconciliation_run_status``
  - ``reconciliation_delta_operation``
  - ``reconciliation_delta_item_status``

Downstream consumers (sync_apply, migration jobs) reuse them via
``Enum(..., create_type=False)``.  Do NOT re-declare these enums elsewhere.

Soft lake references
--------------------
``source_artifact_id`` and ``existing_fact_id`` are plain UUID columns with
NO database-level foreign key.  They point at Iceberg rows
(``raw.access_artifacts`` and ``normalized.access_facts``) whose identity is
maintained by service-level validation, not DB constraints.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base

# ---------------------------------------------------------------------------
# Python StrEnum definitions
# ---------------------------------------------------------------------------


class ReconciliationRunStatus(StrEnum):
    running = 'running'
    pending_apply = 'pending_apply'
    failed = 'failed'
    applied = 'applied'
    partially_applied = 'partially_applied'
    discarded = 'discarded'
    dry_run_completed = 'dry_run_completed'


class ReconciliationDeltaOperation(StrEnum):
    create = 'create'
    update = 'update'
    revoke = 'revoke'
    reactivate = 'reactivate'
    noop = 'noop'


class ReconciliationDeltaItemStatus(StrEnum):
    pending = 'pending'
    approved = 'approved'
    rejected = 'rejected'
    applied = 'applied'
    failed = 'failed'
    ignored = 'ignored'


# ---------------------------------------------------------------------------
# SQLAlchemy Enum column types — owned here, create_type=True
# ---------------------------------------------------------------------------

_run_status_enum = SaEnum(
    ReconciliationRunStatus,
    name='reconciliation_run_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_delta_operation_enum = SaEnum(
    ReconciliationDeltaOperation,
    name='reconciliation_delta_operation',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_delta_item_status_enum = SaEnum(
    ReconciliationDeltaItemStatus,
    name='reconciliation_delta_item_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


class ReconciliationEntityType(StrEnum):
    """Entity type for a reconciliation run / delta item.

    ``access_fact`` — access artifact → access fact (original path).
    ``person``      — raw.persons → persons PG table.
    ``org_unit``    — raw.org_units → org_units PG table.
    ``employee``    — raw.employees → employees PG table.
    """

    access_fact = 'access_fact'
    person = 'person'
    org_unit = 'org_unit'
    employee = 'employee'


_entity_type_enum = SaEnum(
    ReconciliationEntityType,
    name='reconciliation_entity_type',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class ReconciliationRun(Base):
    """One row per reconciliation execution.

    Statuses:
    - ``running``           — pipeline executing, no delta persisted yet.
    - ``pending_apply``     — delta computed and persisted; awaiting Sync/Apply.
    - ``dry_run_completed`` — delta computed and persisted; no apply will happen.
    - ``failed``            — pipeline error; see ``error`` column.
    - ``applied``           — Sync/Apply completed all delta items.
    - ``partially_applied`` — Sync/Apply completed some delta items; remainder failed.
    - ``discarded``         — run explicitly discarded before apply.

    No relationship() declarations — cross-slice joins are explicit.
    """

    __tablename__ = 'reconciliation_runs'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # FK dropped in Phase 15 Step 14 migration to allow NULL for synthetic migration runs.
        # nullable=True is required for cross-app migration provenance (application_id=NULL).
        nullable=True,
    )
    observed_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('lake_batches.id', ondelete='SET NULL'),
        nullable=True,
    )
    observed_snapshot_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        nullable=True,
        comment='Iceberg snapshot id read from raw.access_artifacts at reconciliation time.',
    )
    current_snapshot_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        nullable=True,
        comment='Iceberg snapshot id read from normalized.access_facts at reconciliation time.',
    )
    entity_type: Mapped[ReconciliationEntityType] = mapped_column(
        _entity_type_enum,
        nullable=False,
        default=ReconciliationEntityType.access_fact,
        server_default='access_fact',
    )
    status: Mapped[ReconciliationRunStatus] = mapped_column(
        _run_status_enum,
        nullable=False,
        default=ReconciliationRunStatus.running,
        server_default='running',
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    created_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    updated_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    revoked_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    unchanged_count: Mapped[int] = mapped_column(
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
        sa.Index('ix_reconciliation_runs_application_id', 'application_id'),
        sa.Index('ix_reconciliation_runs_status', 'status'),
    )


class ReconciliationDeltaItem(Base):
    """One row per per-record diff produced by a reconciliation run.

    Consumed by Sync/Apply (Step 12) which reads items with
    status=``pending`` and writes back ``applied`` or ``failed``.

    Soft lake references (NO DB FK):
    - ``source_artifact_id`` → ``raw.access_artifacts`` Iceberg row
    - ``existing_fact_id``   → ``normalized.access_facts`` Iceberg row

    No relationship() declarations — cross-slice joins are explicit.
    """

    __tablename__ = 'reconciliation_delta_items'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('reconciliation_runs.id', ondelete='CASCADE'),
        nullable=False,
    )
    entity_type: Mapped[ReconciliationEntityType] = mapped_column(
        _entity_type_enum,
        nullable=False,
        default=ReconciliationEntityType.access_fact,
        server_default='access_fact',
    )
    operation: Mapped[ReconciliationDeltaOperation] = mapped_column(
        _delta_operation_enum,
        nullable=False,
    )
    # --- access_fact-specific fields (NULL for person/org_unit/employee rows) ---
    natural_key_hash: Mapped[str | None] = mapped_column(
        sa.CHAR(64),
        nullable=True,
        comment='SHA-256 hex of canonical 6-tuple (access_fact only).',
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    action_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        nullable=True,
    )
    effect: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    # --- master data-specific fields (NULL for access_fact rows) ---
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment='PG primary key of the affected master data row (person/org_unit/employee).',
    )
    # Soft lake references — NO DB FK (Iceberg rows have no PG identity)
    existing_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment='Points at normalized.access_facts Iceberg row; no DB FK.',
    )
    source_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment='Points at raw.access_artifacts Iceberg row; no DB FK.',
    )
    before_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment='Fact snapshot before the delta operation.',
    )
    after_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment='Fact snapshot after the delta operation.',
    )
    status: Mapped[ReconciliationDeltaItemStatus] = mapped_column(
        _delta_item_status_enum,
        nullable=False,
        default=ReconciliationDeltaItemStatus.pending,
        server_default='pending',
    )
    reason: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    applied_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        # Composite index supports paginated filtering by status within a run
        # (Step 9 GET /reconciliation/runs/{id}/delta-items and Step 12 apply-recovery scan).
        # The single-column index on reconciliation_run_id is NOT added separately —
        # PG can use the leading column of a composite index for equality lookups,
        # so a separate ix_reconciliation_delta_items_run_id would be redundant.
        sa.Index(
            'ix_reconciliation_delta_items_run_status',
            'reconciliation_run_id',
            'status',
        ),
    )
