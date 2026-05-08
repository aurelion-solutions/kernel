# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationControl ORM model — reference catalog for Phase 13 SoD mitigation controls.

``mitigation_control_type`` Postgres enum is OWNED by this step (create_type=True).
Downstream steps that reference it must use ``Enum(..., create_type=False)``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class MitigationControlType(StrEnum):
    attestation = 'attestation'
    dual_approval = 'dual_approval'
    logging_alerting = 'logging_alerting'
    compensating_process = 'compensating_process'
    other = 'other'


# SQLAlchemy Enum type — owned here, create_type=True
_mitigation_control_type_enum = SaEnum(
    MitigationControlType,
    name='mitigation_control_type',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


class MitigationControl(Base):
    """Reference catalog entry for a mitigation control.

    ``code`` is immutable after creation — Step 9's Mitigation rows will reference
    controls by FK (control_id). If a control must be renamed, deprecate the old
    code and create a new entry.
    """

    __tablename__ = 'mitigation_controls'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    code: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    type: Mapped[MitigationControlType] = mapped_column(
        _mitigation_control_type_enum,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=True,
        server_default=sa.text('true'),
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
        sa.UniqueConstraint('code', name='uq_mitigation_controls_code'),
        sa.Index('ix_mitigation_controls_is_active', 'is_active'),
        sa.Index('ix_mitigation_controls_type', 'type'),
    )
