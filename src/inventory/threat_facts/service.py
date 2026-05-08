# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ThreatFact service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.threat_facts.models import ThreatFact
from src.inventory.threat_facts.repository import (
    get_threat_fact_by_id as repo_get_by_id,
)
from src.inventory.threat_facts.repository import (
    list_threat_facts as repo_list,
)
from src.inventory.threat_facts.repository import (
    upsert_threat_fact as repo_upsert,
)
from src.inventory.threat_facts.schemas import ThreatFactUpsert
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.threat_facts'


class ThreatFactNotFoundError(Exception):
    """Raised when a threat fact is not found by id."""

    def __init__(self, fact_id: uuid.UUID) -> None:
        self.fact_id = fact_id
        super().__init__(f'Threat fact not found: {fact_id}')


class ThreatFactSubjectNotFoundError(Exception):
    """Raised when the referenced subject_id does not exist."""

    def __init__(self, subject_id: uuid.UUID) -> None:
        self.subject_id = subject_id
        super().__init__(f'Subject not found: {subject_id}')


class ThreatFactAccountNotFoundError(Exception):
    """Raised when the referenced account_id does not exist."""

    def __init__(self, account_id: uuid.UUID) -> None:
        self.account_id = account_id
        super().__init__(f'Account not found: {account_id}')


class ThreatFactConflictError(Exception):
    """Raised on concurrent upsert race (23505) or CHECK violation (23514)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ThreatFactService:
    """Orchestrates threat fact operations and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def upsert_threat_fact(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID,
        payload: ThreatFactUpsert,
        correlation_id: str | None = None,
    ) -> tuple[ThreatFact, bool]:
        """Upsert a threat fact. Validates FK existence and maps DB errors."""
        from src.inventory.subjects.models import Subject

        subject = await session.get(Subject, subject_id)
        if subject is None:
            raise ThreatFactSubjectNotFoundError(subject_id)

        if payload.account_id is not None:
            from src.inventory.accounts.models import Account

            account = await session.get(Account, payload.account_id)
            if account is None:
                raise ThreatFactAccountNotFoundError(payload.account_id)

        observed_at = payload.observed_at or datetime.now(UTC)

        try:
            fact, created = await repo_upsert(
                session,
                subject_id=subject_id,
                account_id=payload.account_id,
                risk_score=payload.risk_score,
                active_indicators=payload.active_indicators,
                last_login_at=payload.last_login_at,
                failed_auth_count=payload.failed_auth_count,
                observed_at=observed_at,
            )
        except IntegrityError as exc:
            await session.rollback()
            pgcode = getattr(exc.orig, 'pgcode', None) or getattr(exc.orig, 'sqlstate', None)
            if pgcode == '23503':
                raise ThreatFactSubjectNotFoundError(subject_id) from exc
            if pgcode == '23505':
                raise ThreatFactConflictError('Concurrent upsert for the same subject_id — retry') from exc
            if pgcode == '23514':
                raise ThreatFactConflictError(
                    'CHECK constraint violated (risk_score range or failed_auth_count sign)'
                ) from exc
            raise

        event_type = 'inventory.threat_fact.created' if created else 'inventory.threat_fact.updated'
        cid = correlation_id if correlation_id is not None else uuid.uuid4().hex
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type=event_type,
                occurred_at=datetime.now(UTC),
                correlation_id=cid,
                causation_id=None,
                payload={
                    'fact_id': str(fact.id),
                    'subject_id': str(subject_id),
                    'account_id': str(payload.account_id) if payload.account_id is not None else None,
                    'risk_score': payload.risk_score,
                    'active_indicators_count': len(payload.active_indicators),
                    'failed_auth_count': payload.failed_auth_count,
                    'observed_at': observed_at.isoformat(),
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(fact.id),
            )
        )
        return fact, created

    async def get_threat_fact(
        self,
        session: AsyncSession,
        fact_id: uuid.UUID,
    ) -> ThreatFact | None:
        """Get threat fact by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_by_id(session, fact_id)

    async def list_threat_facts(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        min_risk_score: float | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ThreatFact]:
        """List threat facts with optional filters. No event emitted."""
        return await repo_list(
            session,
            subject_id=subject_id,
            account_id=account_id,
            min_risk_score=min_risk_score,
            limit=limit,
            offset=offset,
        )
