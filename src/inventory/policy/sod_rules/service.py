# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRule service — business logic for the SodRule slice.

No events and no logs are emitted by this service — Phase 13 event catalog
has no ``sod_rule.*`` events; this slice is configuration, same pattern as Capability.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.policy.sod_rules.exceptions import (
    SodRuleCodeAlreadyExistsError,
    SodRuleNotFoundError,
    SodRuleScopeInvariantError,
    SodRuleScopeKeyNotFoundError,
)
from src.inventory.policy.sod_rules.models import SodRuleScope
from src.inventory.policy.sod_rules.repository import (
    get_sod_rule_by_id,
    insert_sod_rule,
    list_sod_rules,
    update_sod_rule_fields,
    verify_scope_key_id_exists,
)
from src.inventory.policy.sod_rules.schemas import (
    SodRuleCreate,
    SodRulePatch,
    SodRuleRead,
    SodSeverity,
)
from src.platform.logs.service import LogService


def _validate_scope_mode_invariants(
    scope_mode: SodRuleScope,
    scope_key_id: int | None,
) -> None:
    """Enforce scope_mode / scope_key_id consistency invariants.

    Raises SodRuleScopeInvariantError on:
    - GLOBAL + scope_key_id is not None
    - BY_SCOPE_KEY + scope_key_id is None
    """
    if scope_mode == SodRuleScope.global_ and scope_key_id is not None:
        raise SodRuleScopeInvariantError("scope_key_id must be null when scope_mode is 'global'")
    if scope_mode == SodRuleScope.by_scope_key and scope_key_id is None:
        raise SodRuleScopeInvariantError("scope_key_id is required when scope_mode is 'by_scope_key'")


def _translate_insert_integrity_error(exc: IntegrityError, code: str) -> None:
    """Translate IntegrityError from insert into a domain error, or re-raise."""
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint_name: str | None = getattr(asyncpg_exc, 'constraint_name', None)
    if pgcode == '23505' and constraint_name == 'uq_sod_rules_code':
        raise SodRuleCodeAlreadyExistsError(code) from None
    raise exc


class SodRuleService:
    """CRUD service for the SodRule vocabulary.

    ``log_service`` is plumbed for parity with other slices but is not used.
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def create(self, payload: SodRuleCreate) -> SodRuleRead:
        """Create a new SodRule. Raises on scope invariant violations or duplicate code."""
        _validate_scope_mode_invariants(payload.scope_mode, payload.scope_key_id)

        if payload.scope_key_id is not None:
            exists = await verify_scope_key_id_exists(self._session, payload.scope_key_id)
            if not exists:
                raise SodRuleScopeKeyNotFoundError(payload.scope_key_id)

        try:
            rule = await insert_sod_rule(
                self._session,
                code=payload.code,
                name=payload.name,
                description=payload.description,
                severity=payload.severity,
                scope_mode=payload.scope_mode,
                scope_key_id=payload.scope_key_id,
                is_enabled=payload.is_enabled,
                mitigation_allowed=payload.mitigation_allowed,
                created_by=payload.created_by,
            )
        except IntegrityError as exc:
            _translate_insert_integrity_error(exc, payload.code)
        return SodRuleRead.model_validate(rule)  # type: ignore[return-value]

    async def list(
        self,
        *,
        is_enabled: bool | None = None,
        severity: SodSeverity | None = None,
        scope_mode: SodRuleScope | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SodRuleRead]:
        """Return SodRules, optionally filtered."""
        rows = await list_sod_rules(
            self._session,
            is_enabled=is_enabled,
            severity=severity,
            scope_mode=scope_mode,
            limit=limit,
            offset=offset,
        )
        return [SodRuleRead.model_validate(row) for row in rows]

    async def get(self, rule_id: int) -> SodRuleRead:
        """Return a SodRule by id. Raises SodRuleNotFoundError when missing."""
        rule = await get_sod_rule_by_id(self._session, rule_id)
        if rule is None:
            raise SodRuleNotFoundError(rule_id)
        return SodRuleRead.model_validate(rule)

    async def patch(self, rule_id: int, payload: SodRulePatch) -> SodRuleRead:
        """Update provided fields on a SodRule. Re-validates scope invariants.

        Algorithm (per Architect Decision 4):
        1. Load existing rule.
        2. Compute effective post-patch scope_mode + scope_key_id.
        3. Re-validate scope invariants.
        4. Verify new scope_key_id exists if changed.
        5. Apply mutations.
        """
        rule = await get_sod_rule_by_id(self._session, rule_id)
        if rule is None:
            raise SodRuleNotFoundError(rule_id)

        provided = payload.model_dump(exclude_unset=True)

        effective_scope_mode = provided['scope_mode'] if 'scope_mode' in provided else rule.scope_mode
        effective_scope_key_id = (
            provided.get('scope_key_id', rule.scope_key_id) if 'scope_key_id' in provided else rule.scope_key_id
        )

        _validate_scope_mode_invariants(effective_scope_mode, effective_scope_key_id)

        # Verify scope_key_id exists only if it changed and is non-null
        scope_key_id_changed = 'scope_key_id' in provided and provided['scope_key_id'] != rule.scope_key_id
        if effective_scope_key_id is not None and scope_key_id_changed:
            exists = await verify_scope_key_id_exists(self._session, effective_scope_key_id)
            if not exists:
                raise SodRuleScopeKeyNotFoundError(effective_scope_key_id)

        # Determine whether to clear scope_key_id (explicitly set to null)
        clear_scope_key = 'scope_key_id' in provided and provided['scope_key_id'] is None

        rule = await update_sod_rule_fields(
            self._session,
            rule,
            name=provided.get('name'),
            description=provided.get('description'),
            severity=provided.get('severity'),
            scope_mode=provided.get('scope_mode'),
            scope_key_id=provided.get('scope_key_id'),
            is_enabled=provided.get('is_enabled'),
            mitigation_allowed=provided.get('mitigation_allowed'),
            _clear_scope_key=clear_scope_key,
        )
        return SodRuleRead.model_validate(rule)

    async def deactivate(self, rule_id: int) -> SodRuleRead:
        """Soft-delete a SodRule by setting is_enabled=False.

        Idempotent: calling twice still returns is_enabled=False without error.
        Raises SodRuleNotFoundError when the rule does not exist.
        """
        rule = await get_sod_rule_by_id(self._session, rule_id)
        if rule is None:
            raise SodRuleNotFoundError(rule_id)
        rule = await update_sod_rule_fields(
            self._session,
            rule,
            is_enabled=False,
        )
        return SodRuleRead.model_validate(rule)
