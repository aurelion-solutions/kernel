# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person and PersonAttribute models."""

import uuid

import sqlalchemy as sa
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.db.base import Base


class Person(Base):
    """Reusable human profile root in the platform domain."""

    __tablename__ = 'persons'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    attributes: Mapped[list['PersonAttribute']] = relationship(
        'PersonAttribute',
        back_populates='person',
        cascade='all, delete-orphan',
    )


class PersonAttribute(Base):
    """Extensible attribute attached to a Person."""

    __tablename__ = 'person_attributes'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('persons.id', ondelete='CASCADE'),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
    )

    person: Mapped['Person'] = relationship(
        'Person',
        back_populates='attributes',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'person_id',
            'key',
            name='uq_person_attributes_person_id_key',
        ),
    )
