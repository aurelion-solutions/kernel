# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact model — current-state store linking subject to resource via action_id FK."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, Enum, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from src.core.db.base import Base

if TYPE_CHECKING:
    from src.inventory.actions.models import Action as RefAction


class AccessFactEffect(StrEnum):
    """Effect of an access fact: allow or deny."""

    allow = 'allow'
    deny = 'deny'


class AccessFact(Base):
    """Normalized access record linking a subject (via optional account) to a resource.

    Current-state store: one active row per (account_id|subject_id, resource_id, action_id).
    Re-granting access after revoke reactivates the existing row in place (is_active=True,
    revoked_at=NULL) rather than inserting a new row — see service.create_fact().

    Application-scope invariant: when account_id is set, Account.application_id must equal
    Resource.application_id. Enforced in service layer (service.create_fact); not possible
    as a DB CHECK because it crosses tables.

    Two partial unique indexes on active rows (where is_active = true):
      - uq_access_facts_active_account_key: (account_id, resource_id, action_id)
      - uq_access_facts_active_subject_key: (subject_id, resource_id, action_id)

    No evidence column — provenance is carried by ArtifactBinding(target_type='access_fact').
    """

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
    # action_id FK → ref_actions(id) ON DELETE RESTRICT (BigInteger, not UUID — matches ref_actions PK)
    action_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        ForeignKey('ref_actions.id', ondelete='RESTRICT'),
        nullable=False,
    )
    effect: Mapped[AccessFactEffect] = mapped_column(
        Enum(AccessFactEffect, name='access_fact_effect'),
        nullable=False,
    )
    valid_from: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    # Lifecycle columns
    is_active: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        server_default=sa.true(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    # caller-supplied — no server_default (see TASK.md Q8: caller knows the real source timestamp)
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Relationship for slug resolution on read (lazy="raise" to prevent N+1 by default;
    # use selectinload explicitly when slug is needed)
    action_ref: Mapped[RefAction] = relationship(
        'Action',
        foreign_keys='AccessFact.action_id',
        lazy='raise',
    )

    __table_args__ = (
        # Partial unique indexes — active rows only
        Index(
            'uq_access_facts_active_account_key',
            'account_id',
            'resource_id',
            'action_id',
            unique=True,
            postgresql_where=sa.text('account_id IS NOT NULL AND is_active = true'),
        ),
        Index(
            'uq_access_facts_active_subject_key',
            'subject_id',
            'resource_id',
            'action_id',
            unique=True,
            postgresql_where=sa.text('account_id IS NULL AND is_active = true'),
        ),
        # Lookup indexes
        Index('ix_access_facts_subject_id', 'subject_id'),
        Index('ix_access_facts_resource_id', 'resource_id'),
        Index('ix_access_facts_account_id', 'account_id'),
        Index('ix_access_facts_action_id', 'action_id'),
        Index('ix_access_facts_valid_window', 'valid_from', 'valid_until'),
        # Sparse index for revoked-row queries (helps reactivation lookup)
        Index(
            'ix_access_facts_is_active',
            'is_active',
            postgresql_where=sa.text('is_active = false'),
        ),
    )
