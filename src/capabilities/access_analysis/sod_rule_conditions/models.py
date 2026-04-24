# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRuleCondition ORM model + M2M association table.

The association table ``sod_rule_condition_capabilities`` is a plain
``sa.Table`` with no mapped class and no ORM relationship(). Cross-slice
joins go through repository SQL only (per project pattern in access_analysis/).
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base

# ---------------------------------------------------------------------------
# M2M association table (plain Table, no mapped class, no relationship)
# ---------------------------------------------------------------------------

sod_rule_condition_capabilities = sa.Table(
    'sod_rule_condition_capabilities',
    Base.metadata,
    sa.Column('condition_id', sa.BigInteger(), nullable=False),
    sa.Column('capability_id', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint(
        'condition_id',
        'capability_id',
        name='pk_sod_rule_condition_capabilities',
    ),
    sa.ForeignKeyConstraint(
        ['condition_id'],
        ['sod_rule_conditions.id'],
        name='sod_rule_condition_capabilities_condition_id_fkey',
        ondelete='CASCADE',
    ),
    sa.ForeignKeyConstraint(
        ['capability_id'],
        ['capabilities.id'],
        name='sod_rule_condition_capabilities_capability_id_fkey',
        ondelete='RESTRICT',
    ),
    sa.Index(
        'ix_sod_rule_condition_capabilities_capability_id',
        'capability_id',
    ),
)


# ---------------------------------------------------------------------------
# SodRuleCondition mapped class
# ---------------------------------------------------------------------------


class SodRuleCondition(Base):
    """One condition arm of a SoD rule.

    Each condition means: "the evaluated subject must hold at least
    ``min_count`` capabilities from the linked ``capability_ids`` set."

    Conditions are immutable after creation (no PATCH endpoint) — replace
    by DELETE + POST.

    No relationship() declarations — cross-slice joins are explicit.
    """

    __tablename__ = 'sod_rule_conditions'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    rule_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('sod_rules.id', ondelete='CASCADE'),
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(
        sa.String(128),
        nullable=True,
    )
    min_count: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=1,
        server_default=sa.text('1'),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        sa.CheckConstraint(
            'min_count >= 1',
            name='ck_sod_rule_conditions_min_count_positive',
        ),
        sa.Index('ix_sod_rule_conditions_rule_id', 'rule_id'),
    )
