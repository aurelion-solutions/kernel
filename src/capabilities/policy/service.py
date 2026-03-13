# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PolicyService — wraps evaluate() + load_rules_from_yaml() and emits policy.decision.made."""

from pathlib import Path
from typing import Any

from src.capabilities.policy.evaluator import evaluate
from src.capabilities.policy.loader import load_rules_from_yaml
from src.capabilities.policy.schemas import Decision, Facts, RulePack
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import (
    LogService,
    merge_emit_log_participant_fields,
    noop_log_service,
)

_DEFAULT_POLICIES_DIR = Path(__file__).resolve().parents[3] / 'resources' / 'policies'


class PolicyService:
    """Business logic layer for policy evaluation.

    Loads rules once at init time from YAML. Delegates decisions to the pure
    evaluate() function and emits a structured ``policy.decision.made`` event
    via LogService for every decision.
    """

    def __init__(
        self,
        log_service: LogService | None = None,
        policies_dir: Path | None = None,
    ) -> None:
        self._log = log_service if log_service is not None else noop_log_service
        resolved_dir = policies_dir if policies_dir is not None else _DEFAULT_POLICIES_DIR
        self._rule_pack: RulePack = load_rules_from_yaml(resolved_dir)

    def evaluate_policy(self, facts: Facts) -> Decision:
        """Evaluate lifecycle and risk rules against *facts*.

        Combines lifecycle + risk rules, calls evaluate(), emits
        ``policy.decision.made`` via LogService, and returns the Decision.
        """
        all_rules = self._rule_pack.lifecycle + self._rule_pack.risk
        decision = evaluate(all_rules, facts, mapping=self._rule_pack.mapping)

        payload: dict[str, Any] = {
            'subject_id': facts.subject.id,
            'subject_kind': facts.subject.kind,
            'abstract_state': str(decision.abstract_state),
            'actions_count': len(decision.actions),
            'signals_count': len(decision.signals),
            'reasons_count': len(decision.reasons),
            'target_application': facts.target.application if facts.target is not None else None,
        }
        if decision.concrete_state is not None:
            payload['concrete_state'] = decision.concrete_state
        if decision.risk_level is not None:
            payload['risk_level'] = str(decision.risk_level)

        try:
            self._log.emit_safe(
                'policy.decision.made',
                LogLevel.INFO,
                'Policy decision evaluated',
                'policy-engine',
                merge_emit_log_participant_fields(
                    payload,
                    actor_component='policy-engine',
                    target_id='policy',
                ),
            )
        except Exception:
            pass
        return decision

    def get_rule_pack(self) -> RulePack:
        """Return the loaded RulePack (lifecycle + risk rules + mapping)."""
        return self._rule_pack
