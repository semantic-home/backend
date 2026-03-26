from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.controller_store.watering import WateringController
from backend.entities_store.entities import EntityStore
from backend.policy.watering import PolicyFact, evaluate_watering_policy
from backend.rules_store.watering import Rule
from backend.watering_fsm import WateringMode, WateringRuntimeState

_MESSAGE_BY_REASON_CODE: dict[str, str] = {
    "no_rule_configured": "No rule configured.",
}


@dataclass
class WateringWhy:
    controller_id: str
    decision: str
    reason_code: str
    message: str
    at: datetime

    moisture_sensor_entity_ids: list[str]
    moisture_state_raw: str | None
    moisture_value: float | None
    moisture_start_below: float | None


def _parse_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _message_for(reason_code: str) -> str:
    return _MESSAGE_BY_REASON_CODE.get(reason_code, reason_code)


def _policy_outcome_to_why_decision(outcome: str) -> str:
    if outcome == "allow":
        return "start"
    if outcome == "deny":
        return "skip"
    return "unknown"


def _find_numeric_condition_value(rule: Rule, *, field: str) -> float | None:
    for condition in rule.conditions:
        if condition.field != field:
            continue
        return _parse_float(condition.value)
    return None


def _pick_primary_fact(facts: tuple[PolicyFact, ...], *, field: str) -> PolicyFact | None:
    for fact in facts:
        if fact.field == field:
            return fact
    return None


def _compute_rule_based_watering_why(
    controller: WateringController,
    entity_store: EntityStore,
    *,
    rule: Rule,
    runtime_state: WateringRuntimeState | None,
    controller_paused: bool = False,
) -> WateringWhy:
    if controller_paused and (runtime_state is None or runtime_state.mode is WateringMode.OFF):
        return WateringWhy(
            controller_id=controller.controller_id,
            decision="skip",
            reason_code="controller_paused",
            message="Controller is paused.",
            at=datetime.now(timezone.utc),
            moisture_sensor_entity_ids=list(controller.moisture_sensor_entity_ids or []),
            moisture_state_raw=None,
            moisture_value=None,
            moisture_start_below=_find_numeric_condition_value(rule, field="moisture"),
        )

    decision = evaluate_watering_policy(
        rule=rule,
        controller=controller,
        entity_store=entity_store,
        runtime_state=runtime_state,
    )
    moisture_fact = _pick_primary_fact(decision.facts, field="moisture")

    return WateringWhy(
        controller_id=controller.controller_id,
        decision=_policy_outcome_to_why_decision(decision.outcome),
        reason_code=decision.reason_code,
        message=decision.message,
        at=decision.evaluated_at,
        moisture_sensor_entity_ids=list(moisture_fact.source_entity_ids) if moisture_fact else list(controller.moisture_sensor_entity_ids or []),
        moisture_state_raw=moisture_fact.selected_state_raw if moisture_fact else None,
        moisture_value=moisture_fact.selected_value if moisture_fact else None,
        moisture_start_below=_find_numeric_condition_value(rule, field="moisture"),
    )


def compute_watering_why(
    controller: WateringController,
    entity_store: EntityStore,
    *,
    rule: Rule | None = None,
    runtime_state: WateringRuntimeState | None = None,
    controller_paused: bool = False,
) -> WateringWhy:
    """Determines watering decision based on moisture level"""
    if rule is not None:
        return _compute_rule_based_watering_why(
            controller,
            entity_store,
            rule=rule,
            runtime_state=runtime_state,
            controller_paused=controller_paused,
        )

    return WateringWhy(
        controller_id=controller.controller_id,
        decision="unknown",
        reason_code="no_rule_configured",
        message=_message_for("no_rule_configured"),
        at=datetime.now(timezone.utc),
        moisture_sensor_entity_ids=list(controller.moisture_sensor_entity_ids or []),
        moisture_state_raw=None,
        moisture_value=None,
        moisture_start_below=None,
    )
