# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun ORM model + ScanRunStatus / ScanRunTrigger enums.

Both Postgres enum types are OWNED by this step:
  - ``scan_run_status``  (name='scan_run_status',  create_type=True)
  - ``scan_run_trigger`` (name='scan_run_trigger', create_type=True)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class ScanRunStatus(StrEnum):
    pending = 'pending'
    running = 'running'
    completed = 'completed'
    failed = 'failed'


class ScanRunTrigger(StrEnum):
    manual = 'manual'
    api = 'api'
    schedule = 'schedule'


# SQLAlchemy Enum types — owned here, create_type=True
_scan_run_status_enum = SaEnum(
    ScanRunStatus,
    name='scan_run_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_scan_run_trigger_enum = SaEnum(
    ScanRunTrigger,
    name='scan_run_trigger',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


class ScanRun(Base):
    """One row per evaluation pass.

    No relationship() declarations — cross-slice joins are explicit.
    """

    __tablename__ = 'scan_runs'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    status: Mapped[ScanRunStatus] = mapped_column(
        _scan_run_status_enum,
        nullable=False,
        default=ScanRunStatus.pending,
        server_default='pending',
    )
    triggered_by: Mapped[ScanRunTrigger] = mapped_column(
        _scan_run_trigger_enum,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    scope_subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=True,
    )
    scope_application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('applications.id', ondelete='RESTRICT'),
        nullable=True,
    )
    findings_total: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    findings_by_severity: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default='{}',
    )
    findings_created_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    findings_reused_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default='0',
    )
    error_message: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(
        sa.String(255),
        nullable=True,
    )

    __table_args__ = (
        sa.CheckConstraint(
            "completed_at IS NULL OR status IN ('completed', 'failed')",
            name='ck_scan_runs_completed_at_terminal',
        ),
        sa.CheckConstraint(
            "started_at IS NOT NULL OR status = 'pending'",
            name='ck_scan_runs_started_at_not_pending',
        ),
        sa.CheckConstraint(
            'findings_total >= 0',
            name='ck_scan_runs_findings_total_nonneg',
        ),
        sa.CheckConstraint(
            'findings_created_count >= 0',
            name='ck_scan_runs_findings_created_count_nonneg',
        ),
        sa.CheckConstraint(
            'findings_reused_count >= 0',
            name='ck_scan_runs_findings_reused_count_nonneg',
        ),
        sa.Index('ix_scan_runs_status', 'status'),
        sa.Index('ix_scan_runs_created_at_desc', sa.text('created_at DESC')),
        sa.Index('ix_scan_runs_scope_subject_id', 'scope_subject_id'),
        sa.Index('ix_scan_runs_scope_application_id', 'scope_application_id'),
    )
