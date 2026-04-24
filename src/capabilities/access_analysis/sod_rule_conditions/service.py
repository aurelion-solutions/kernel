# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRuleCondition service — business logic for the SodRuleCondition slice.

No events and no logs are emitted — Phase 13 event catalog has no condition events.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.sod_rule_conditions.exceptions import (
    SodRuleConditionCapabilityNotFoundError,
    SodRuleConditionEmptyCapabilitiesError,
    SodRuleConditionNotFoundError,
)
from src.capabilities.access_analysis.sod_rule_conditions.repository import (
    delete_sod_rule_condition,
    get_sod_rule_condition_by_id_with_capabilities,
    insert_sod_rule_condition_with_capabilities,
    list_sod_rule_conditions_for_rule,
    verify_capability_ids_exist,
    verify_rule_id_exists,
)
from src.capabilities.access_analysis.sod_rule_conditions.schemas import (
    SodRuleConditionCreate,
    SodRuleConditionRead,
)
from src.capabilities.access_analysis.sod_rules.exceptions import SodRuleNotFoundError
from src.platform.logs.service import LogService


class SodRuleConditionService:
    """CRUD service for the SodRuleCondition slice.

    ``log_service`` is plumbed for parity but unused — no events for condition CRUD.
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def create(self, rule_id: int, payload: SodRuleConditionCreate) -> SodRuleConditionRead:
        """Create a new condition for the given rule.

        Raises:
            SodRuleNotFoundError: when rule_id does not exist.
            SodRuleConditionEmptyCapabilitiesError: when capability_ids is empty.
            SodRuleConditionCapabilityNotFoundError: when any capability_id is missing.
        """
        # Validate rule exists
        rule_exists = await verify_rule_id_exists(self._session, rule_id)
        if not rule_exists:
            raise SodRuleNotFoundError(rule_id)

        # Validate capability_ids non-empty (belt-and-suspenders; schema also validates)
        if not payload.capability_ids:
            raise SodRuleConditionEmptyCapabilitiesError()

        # Validate all capability_ids exist
        missing_ids = await verify_capability_ids_exist(self._session, payload.capability_ids)
        if missing_ids:
            raise SodRuleConditionCapabilityNotFoundError(missing_ids)

        row = await insert_sod_rule_condition_with_capabilities(
            self._session,
            rule_id=rule_id,
            name=payload.name,
            min_count=payload.min_count,
            capability_ids=payload.capability_ids,
        )
        return SodRuleConditionRead.from_row(row)

    async def list_for_rule(self, rule_id: int) -> list[SodRuleConditionRead]:
        """Return all conditions for a rule ordered by id ASC."""
        rows = await list_sod_rule_conditions_for_rule(self._session, rule_id)
        return [SodRuleConditionRead.from_row(row) for row in rows]

    async def get(self, condition_id: int) -> SodRuleConditionRead:
        """Return a condition by id. Raises SodRuleConditionNotFoundError when missing."""
        row = await get_sod_rule_condition_by_id_with_capabilities(self._session, condition_id)
        if row is None:
            raise SodRuleConditionNotFoundError(condition_id)
        return SodRuleConditionRead.from_row(row)

    async def delete(self, condition_id: int) -> None:
        """Delete a condition by id. Raises SodRuleConditionNotFoundError when missing."""
        deleted = await delete_sod_rule_condition(self._session, condition_id)
        if not deleted:
            raise SodRuleConditionNotFoundError(condition_id)
