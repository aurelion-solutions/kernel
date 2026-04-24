# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Finding ORM model + FindingKind / FindingStatus enums.

Both Postgres enum types are OWNED by this step:
  - ``finding_kind``   (name='finding_kind',   create_type=True)
  - ``finding_status`` (name='finding_status', create_type=True)

``SodSeverity`` (name='sod_severity') is REUSED from the sod_rules slice —
uses Enum(..., create_type=False) so SQLAlchemy does not attempt to re-create it.
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
from src.capabilities.access_analysis.sod_rules.models import SodSeverity
from src.core.db.base import Base


class FindingKind(StrEnum):
    sod = 'sod'
    orphan_access = 'orphan_access'
    terminated_access = 'terminated_access'
    unused_access = 'unused_access'


class FindingStatus(StrEnum):
    open = 'open'
    acknowledged = 'acknowledged'
    resolved = 'resolved'
    mitigated = 'mitigated'


# SQLAlchemy Enum types — owned here, create_type=True
_finding_kind_enum = SaEnum(
    FindingKind,
    name='finding_kind',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_finding_status_enum = SaEnum(
    FindingStatus,
    name='finding_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

# SodSeverity reused — create_type=False to avoid re-creating the type
_sod_severity_col = SaEnum(
    SodSeverity,
    name='sod_severity',
    create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class Finding(Base):
    """One row per detected violation/anomaly.

    No relationship() declarations — cross-slice joins are explicit.
    No FindingCreate API — findings are written by the engine (Step 14);
    CRUD-create is intentionally absent from the public API.
    """

    __tablename__ = 'findings'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    scan_run_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('scan_runs.id', ondelete='RESTRICT'),
        nullable=False,
    )
    kind: Mapped[FindingKind] = mapped_column(
        _finding_kind_enum,
        nullable=False,
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('ent_accounts.id', ondelete='RESTRICT'),
        nullable=True,
    )
    rule_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('sod_rules.id', ondelete='RESTRICT'),
        nullable=True,
    )
    scope_key_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capability_scope_keys.id', ondelete='RESTRICT'),
        nullable=True,
    )
    scope_value: Mapped[str | None] = mapped_column(
        sa.String(255),
        nullable=True,
    )
    severity: Mapped[SodSeverity] = mapped_column(
        _sod_severity_col,
        nullable=False,
    )
    status: Mapped[FindingStatus] = mapped_column(
        _finding_status_enum,
        nullable=False,
        default=FindingStatus.open,
        server_default='open',
    )
    matched_capability_grant_ids: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default='[]',
    )
    matched_effective_grant_ids: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default='[]',
    )
    matched_access_fact_ids: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default='[]',
    )
    evidence_hash: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
    )
    # active_mitigation_id and proposed_mitigation_id are plain BigInteger columns with no FK.
    # The FK constraint will be added by the Step 9 migration when the mitigations table ships.
    active_mitigation_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        nullable=True,
    )
    proposed_mitigation_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        nullable=True,
    )
    detected_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    status_reason: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )

    __table_args__ = (
        # Uniqueness: full finding identity
        sa.UniqueConstraint(
            'kind',
            'subject_id',
            'account_id',
            'rule_id',
            'scope_key_id',
            'scope_value',
            'evidence_hash',
            name='uq_findings_evidence',
        ),
        # CHECK: sod kind ↔ rule_id set
        sa.CheckConstraint(
            "(kind = 'sod') = (rule_id IS NOT NULL)",
            name='ck_findings_rule_id_for_sod',
        ),
        # CHECK: at least one anchor
        sa.CheckConstraint(
            'subject_id IS NOT NULL OR account_id IS NOT NULL',
            name='ck_findings_subject_or_account',
        ),
        # CHECK: orphan_access has no subject
        sa.CheckConstraint(
            "kind <> 'orphan_access' OR subject_id IS NULL",
            name='ck_findings_orphan_no_subject',
        ),
        # Indexes
        sa.Index('ix_findings_subject_status', 'subject_id', 'status'),
        sa.Index('ix_findings_rule_status', 'rule_id', 'status'),
        sa.Index(
            'ix_findings_kind_status_detected',
            'kind',
            'status',
            sa.text('detected_at DESC'),
        ),
        sa.Index('ix_findings_severity_status', 'severity', 'status'),
        sa.Index('ix_findings_active_mitigation_id', 'active_mitigation_id'),
        sa.Index('ix_findings_proposed_mitigation_id', 'proposed_mitigation_id'),
        sa.Index('ix_findings_scan_run_id', 'scan_run_id'),
    )
