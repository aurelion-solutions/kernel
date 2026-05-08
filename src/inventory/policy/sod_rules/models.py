# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRule ORM model + SodSeverity / SodRuleScope enums.

Both Postgres enum types are OWNED by this step:
  - ``sod_severity``   (name='sod_severity',   create_type=True)
  - ``sod_rule_scope`` (name='sod_rule_scope',  create_type=True)

Downstream steps that reference them must use ``Enum(..., create_type=False)``
so SQLAlchemy does not attempt to re-create the types.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class SodSeverity(StrEnum):
    critical = 'critical'
    high = 'high'
    medium = 'medium'
    low = 'low'
    informational = 'informational'


class SodRuleScope(StrEnum):
    global_ = 'global'
    per_application = 'per_application'
    by_scope_key = 'by_scope_key'


# SQLAlchemy Enum types — owned here, create_type=True
_sod_severity_enum = SaEnum(
    SodSeverity,
    name='sod_severity',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)

_sod_rule_scope_enum = SaEnum(
    SodRuleScope,
    name='sod_rule_scope',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


class SodRule(Base):
    """SoD rule header — code, severity, scope configuration, and enablement.

    No relationship() declarations — cross-slice joins are explicit.
    Hard DELETE is not exposed via the API; rules are soft-deleted via ``deactivate``.
    """

    __tablename__ = 'sod_rules'

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
    severity: Mapped[SodSeverity] = mapped_column(
        _sod_severity_enum,
        nullable=False,
    )
    scope_mode: Mapped[SodRuleScope] = mapped_column(
        _sod_rule_scope_enum,
        nullable=False,
    )
    scope_key_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capability_scope_keys.id', ondelete='RESTRICT'),
        nullable=True,
    )
    is_enabled: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=True,
        server_default=sa.text('true'),
    )
    mitigation_allowed: Mapped[bool] = mapped_column(
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
        sa.UniqueConstraint('code', name='uq_sod_rules_code'),
        sa.CheckConstraint(
            "scope_mode <> 'global' OR scope_key_id IS NULL",
            name='ck_sod_rules_scope_key_global',
        ),
        sa.CheckConstraint(
            "scope_mode <> 'by_scope_key' OR scope_key_id IS NOT NULL",
            name='ck_sod_rules_scope_key_by_scope_key',
        ),
        sa.Index('ix_sod_rules_is_enabled', 'is_enabled'),
        sa.Index('ix_sod_rules_scope_mode', 'scope_mode'),
    )
