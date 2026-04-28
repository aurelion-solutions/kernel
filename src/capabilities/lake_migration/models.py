# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LakeMigrationRun ORM model for tracking PG → Iceberg migration jobs.

Two PG enums owned here (``create_type=True``):
  - ``lake_migration_dataset``  (access_artifacts, access_facts)
  - ``lake_migration_status``   (pending, running, completed, failed, cancelled)

Soft reference: ``synthetic_run_id`` points at ``reconciliation_runs.id`` with
NO database FK — synthetic runs are identified by ``reason='pg_migration'`` and
the referenced row may differ per dataset.  DB constraint would require
``reconciliation_runs.application_id`` to be nullable BEFORE the FK is added,
which is handled in the Step 14 Alembic migration.
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


class LakeMigrationDataset(StrEnum):
    access_artifacts = 'access_artifacts'
    access_facts = 'access_facts'


class LakeMigrationStatus(StrEnum):
    pending = 'pending'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
    cancelled = 'cancelled'


# ---------------------------------------------------------------------------
# SQLAlchemy Enum column types — owned here, create_type=True
# ---------------------------------------------------------------------------

_dataset_enum = SaEnum(
    LakeMigrationDataset,
    name='lake_migration_dataset',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_status_enum = SaEnum(
    LakeMigrationStatus,
    name='lake_migration_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


# ---------------------------------------------------------------------------
# ORM Model
# ---------------------------------------------------------------------------


class LakeMigrationRun(Base):
    """One row per migration job execution.

    Columns:
    - ``id``                 — UUID PK.
    - ``dataset``            — which dataset is being migrated.
    - ``status``             — lifecycle state.
    - ``started_at``         — set when status transitions to ``running``.
    - ``finished_at``        — set when status reaches terminal state.
    - ``created_at``         — server-side ``now()`` at INSERT.
    - ``last_processed_id``  — checkpoint cursor; updated per batch.
    - ``rows_read``          — cumulative rows read from PG.
    - ``rows_written``       — cumulative rows written to Iceberg.
    - ``batch_size``         — configured batch size.
    - ``error``              — error message on failure.
    - ``synthetic_run_id``   — soft ref to ``reconciliation_runs.id``; NO DB FK.
    - ``lake_batch_id``      — FK to ``lake_batches.id`` ON DELETE RESTRICT.
    - ``metadata_json``      — arbitrary provenance metadata (JSONB).

    No relationship() declarations — cross-slice joins are explicit.
    """

    __tablename__ = 'lake_migration_runs'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    dataset: Mapped[LakeMigrationDataset] = mapped_column(
        _dataset_enum,
        nullable=False,
    )
    status: Mapped[LakeMigrationStatus] = mapped_column(
        _status_enum,
        nullable=False,
        default=LakeMigrationStatus.pending,
        server_default='pending',
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
    last_processed_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment='Cursor checkpoint: last PG row id processed (UUID of artifact or fact).',
    )
    rows_read: Mapped[int] = mapped_column(
        sa.BigInteger(),
        nullable=False,
        default=0,
        server_default='0',
    )
    rows_written: Mapped[int] = mapped_column(
        sa.BigInteger(),
        nullable=False,
        default=0,
        server_default='0',
    )
    batch_size: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=5000,
        server_default='5000',
    )
    error: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    # Soft ref — NO DB FK (reconciliation_runs may have NULL application_id after Step 14 migration)
    synthetic_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment='Soft ref to reconciliation_runs.id; no DB FK.',
    )
    lake_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('lake_batches.id', ondelete='RESTRICT'),
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    __table_args__ = (
        sa.Index('ix_lake_migration_runs_dataset_status', 'dataset', 'status'),
        sa.Index('ix_lake_migration_runs_created_at', 'created_at'),
    )
