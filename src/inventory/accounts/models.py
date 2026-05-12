# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account model for reconciled remote accounts."""

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base


class AccountStatus(StrEnum):
    """Closed vocabulary for Account.status. Single source of truth."""

    active = 'active'
    suspended = 'suspended'
    disabled = 'disabled'
    deleted = 'deleted'
    unknown = 'unknown'
    invited = 'invited'


class Account(Base):
    """Normalized remote account reconciled from connector payloads."""

    __tablename__ = 'ent_accounts'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='CASCADE'),
        nullable=False,
    )

    username: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    display_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    email: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        default=True,
        nullable=False,
        server_default=sa.text('true'),
    )

    is_privileged: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
        server_default=sa.text('false'),
    )

    mfa_enabled: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
        server_default=sa.text('false'),
    )

    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {},
        server_default=sa.text("'{}'::jsonb"),
    )

    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('subjects.id', ondelete='SET NULL'),
        nullable=True,
    )

    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus, name='account_status'),
        nullable=False,
        default=AccountStatus.unknown,
        server_default=sa.text("'unknown'"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index('ix_ent_accounts_subject_id', 'subject_id'),
        Index('ix_ent_accounts_app_username', 'application_id', 'username', unique=True),
    )
