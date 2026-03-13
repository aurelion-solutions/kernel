# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OwnershipAssignment model — binds a Subject (owner) to exactly one Resource XOR Account."""

from __future__ import annotations

import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class OwnershipKind(str, enum.Enum):
    """Closed vocabulary for ownership relationship kind."""

    primary = 'primary'
    secondary = 'secondary'
    technical = 'technical'


class OwnershipAssignment(Base):
    """Links a Subject (owner) to exactly one Resource or Account."""

    __tablename__ = 'ownership_assignments'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('subjects.id', ondelete='CASCADE'),
        nullable=False,
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('resources.id', ondelete='CASCADE'),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('ent_accounts.id', ondelete='CASCADE'),
        nullable=True,
    )
    kind: Mapped[OwnershipKind] = mapped_column(
        sa.Enum(OwnershipKind, name='ownership_kind', create_type=False),
        nullable=False,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            '(resource_id IS NULL) != (account_id IS NULL)',
            name='chk_ownership_assignment_xor_target',
        ),
        UniqueConstraint(
            'subject_id',
            'resource_id',
            'kind',
            name='uq_ownership_assignments_subject_resource_kind',
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint(
            'subject_id',
            'account_id',
            'kind',
            name='uq_ownership_assignments_subject_account_kind',
            postgresql_nulls_not_distinct=True,
        ),
        Index('ix_ownership_assignments_subject_id', 'subject_id'),
        Index('ix_ownership_assignments_resource_id', 'resource_id'),
        Index('ix_ownership_assignments_account_id', 'account_id'),
        Index('ix_ownership_assignments_kind', 'kind'),
    )
