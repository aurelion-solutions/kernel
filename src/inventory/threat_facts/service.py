# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ThreatFact service — business logic and operational log emission."""

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
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

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
    """Orchestrates threat fact operations and operational log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def upsert_threat_fact(
        self,
        session: AsyncSession,
        *,
        subject_id: uuid.UUID,
        payload: ThreatFactUpsert,
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

        event_type = 'threat_fact.created' if created else 'threat_fact.updated'
        self._log.emit_safe(
            event_type,
            LogLevel.INFO,
            f'Threat fact {"created" if created else "updated"}',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'fact_id': str(fact.id),
                    'subject_id': str(subject_id),
                    'account_id': str(payload.account_id) if payload.account_id is not None else None,
                    'risk_score': payload.risk_score,
                    'active_indicators_count': len(payload.active_indicators),
                    'failed_auth_count': payload.failed_auth_count,
                    'observed_at': observed_at.isoformat(),
                },
                actor_component=_COMPONENT,
                target_id='threat_fact',
            ),
        )
        return fact, created

    async def get_threat_fact(
        self,
        session: AsyncSession,
        fact_id: uuid.UUID,
    ) -> ThreatFact | None:
        """Get threat fact by id. Logs retrieval when found."""
        fact = await repo_get_by_id(session, fact_id)
        if fact is not None:
            self._log.emit_safe(
                'threat_fact.retrieved',
                LogLevel.INFO,
                'Threat fact retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {
                        'fact_id': str(fact_id),
                        'subject_id': str(fact.subject_id),
                    },
                    actor_component=_COMPONENT,
                    target_id='threat_fact',
                ),
            )
        return fact

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
        """List threat facts with optional filters. No logging."""
        return await repo_list(
            session,
            subject_id=subject_id,
            account_id=account_id,
            min_risk_score=min_risk_score,
            limit=limit,
            offset=offset,
        )
