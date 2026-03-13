# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact model — normalized access record linking subject to resource with action and effect."""

from __future__ import annotations

from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base
from src.inventory.enums import Action


class AccessFactEffect(StrEnum):
    """Effect of an access fact: allow or deny."""

    allow = 'allow'
    deny = 'deny'


class AccessFact(Base):
    """Normalized access record linking a subject (via optional account) to a resource."""

    __tablename__ = 'access_facts'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('ent_accounts.id', ondelete='SET NULL'),
        nullable=True,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('resources.id', ondelete='RESTRICT'),
        nullable=False,
    )
    action: Mapped[Action] = mapped_column(
        Enum(Action, name='action', create_type=False),
        nullable=False,
    )
    effect: Mapped[AccessFactEffect] = mapped_column(
        Enum(AccessFactEffect, name='access_fact_effect'),
        nullable=False,
    )
    valid_from: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    valid_until: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            'subject_id',
            'account_id',
            'resource_id',
            'action',
            'effect',
            name='uq_access_facts_natural_key',
            postgresql_nulls_not_distinct=True,
        ),
        Index('ix_access_facts_subject_id', 'subject_id'),
        Index('ix_access_facts_resource_id', 'resource_id'),
        Index('ix_access_facts_account_id', 'account_id'),
        Index('ix_access_facts_action', 'action'),
        Index('ix_access_facts_valid_window', 'valid_from', 'valid_until'),
    )
