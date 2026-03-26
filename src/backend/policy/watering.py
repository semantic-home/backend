from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from backend.controller_store.watering import WateringController
from backend.entities_store.entities import EntityStore
from backend.rules_store.watering import Condition, Rule
from backend.scheduler.watering import compute_rule_next_run_at, effective_next_run_at
from backend.watering_fsm import WateringMode, WateringRuntimeState

FactStatus = Literal["ok", "missing", "invalid", "unsupported"]
PolicyOutcome = Literal["allow", "deny", "unknown"]

MAX_POLICY_WATERING_SECONDS = 3600

_FIELD_LABELS: dict[str, str] = {
    "moisture": "moisture",
    "tank_level": "tank level",
}

_OPERATOR_LABELS: dict[str, str] = {
    "<": "is below",
    ">": "is above",
    "<=": "is at most",
    ">=": "is at least",
    "=": "equals",
}

_NEGATED_OPERATOR_LABELS: dict[str, str] = {
    "<": "is not below",
    ">": "is not above",
    "<=": "is greater than",
    ">=": "is below",
    "=": "does not equal",
}


@dataclass(frozen=True)
class PolicyFact:
    field: str
    status: FactStatus
    message: str
    source_entity_ids: tuple[str, ...] = ()
    selected_entity_id: str | None = None
    selected_state_raw: str | None = None
    selected_value: float | None = None
    comparison_passed: bool | None = None
    operator: str | None = None
    expected_value: int | float | str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class WateringPolicyDecision:
    outcome: PolicyOutcome
    reason_code: str
    message: str
    evaluated_at: datetime
    effective_seconds: int | None = None
    facts: tuple[PolicyFact, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WateringRulePreview:
    controller_id: str
    summary: str
    next_run_at: datetime | None
    decision: WateringPolicyDecision


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _format_number(value: int | float | str) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _build_condition_summary(condition: Condition) -> str:
    label = _FIELD_LABELS.get(condition.field, condition.field)
    op_label = _OPERATOR_LABELS.get(condition.op, condition.op)
    suffix = condition.unit or ""
    return f"{label} {op_label} {_format_number(condition.value)}{suffix}"


def summarize_rule(rule: Rule, *, effective_seconds: int | None = None) -> str:
    if rule.schedule.type == "daily":
        schedule_summary = "every day"
    elif rule.schedule.type == "weekly":
        days_summary = ", ".join(rule.schedule.days) if rule.schedule.days else "selected days"
        schedule_summary = f"on {days_summary}"
    else:
        schedule_summary = f"on {rule.schedule.type}"

    if rule.conditions:
        conditions_summary = " and ".join(_build_condition_summary(condition) for condition in rule.conditions)
    else:
        conditions_summary = "no conditions configured"

    seconds = effective_seconds if effective_seconds is not None else rule.action.seconds
    seconds_label = "second" if seconds == 1 else "seconds"

    return (
        f"Start watering for {seconds} {seconds_label} "
        f"at {rule.schedule.at} ({rule.schedule.tz}) {schedule_summary} "
        f"if {conditions_summary}."
    )


def _decision(
    *,
    outcome: PolicyOutcome,
    reason_code: str,
    message: str,
    evaluated_at: datetime,
    effective_seconds: int | None = None,
    facts: tuple[PolicyFact, ...] = (),
) -> WateringPolicyDecision:
    return WateringPolicyDecision(
        outcome=outcome,
        reason_code=reason_code,
        message=message,
        evaluated_at=evaluated_at,
        effective_seconds=effective_seconds,
        facts=facts,
    )


def _resolve_moisture_fact(controller: WateringController, condition: Condition, entity_store: EntityStore) -> PolicyFact:
    sensor_ids = tuple(controller.moisture_sensor_entity_ids or [])
    if not sensor_ids:
        return PolicyFact(
            field=condition.field,
            status="missing",
            message="No moisture sensor is linked to this controller.",
            source_entity_ids=sensor_ids,
            operator=condition.op,
            expected_value=condition.value,
            unit=condition.unit,
        )

    expected_value = _parse_float(condition.value)
    if expected_value is None:
        return PolicyFact(
            field=condition.field,
            status="invalid",
            message=f"Condition value '{condition.value}' is not numeric.",
            source_entity_ids=sensor_ids,
            operator=condition.op,
            expected_value=condition.value,
            unit=condition.unit,
        )

    if condition.op not in _OPERATOR_LABELS:
        return PolicyFact(
            field=condition.field,
            status="unsupported",
            message=f"Operator '{condition.op}' is not supported.",
            source_entity_ids=sensor_ids,
            operator=condition.op,
            expected_value=condition.value,
            unit=condition.unit,
        )

    readings: list[tuple[str, str, float]] = []
    missing_sensor_ids: list[str] = []
    invalid_sensor_ids: list[str] = []

    for sensor_id in sensor_ids:
        rec = entity_store.get(sensor_id)
        if rec is None:
            missing_sensor_ids.append(sensor_id)
            continue

        raw_value = rec.state
        parsed = _parse_float(raw_value)
        if parsed is None:
            invalid_sensor_ids.append(sensor_id)
            continue
        readings.append((sensor_id, raw_value, parsed))

    if not readings:
        if missing_sensor_ids:
            return PolicyFact(
                field=condition.field,
                status="missing",
                message=f"Missing live moisture data for {', '.join(missing_sensor_ids)}.",
                source_entity_ids=sensor_ids,
                operator=condition.op,
                expected_value=condition.value,
                unit=condition.unit,
            )

        return PolicyFact(
            field=condition.field,
            status="invalid",
            message=f"Moisture values are not numeric for {', '.join(invalid_sensor_ids)}.",
            source_entity_ids=sensor_ids,
            operator=condition.op,
            expected_value=condition.value,
            unit=condition.unit,
        )

    sensor_id, raw_value, selected_value = min(readings, key=lambda item: item[2])
    comparisons = {
        "<": selected_value < expected_value,
        ">": selected_value > expected_value,
        "<=": selected_value <= expected_value,
        ">=": selected_value >= expected_value,
        "=": selected_value == expected_value,
    }
    comparison_passed = comparisons[condition.op]
    unit_suffix = condition.unit or ""
    comparator = _OPERATOR_LABELS[condition.op] if comparison_passed else _NEGATED_OPERATOR_LABELS[condition.op]

    return PolicyFact(
        field=condition.field,
        status="ok",
        message=(
            f"{sensor_id}: {selected_value:.1f}{unit_suffix} {comparator} "
            f"{expected_value:.1f}{unit_suffix}."
        ),
        source_entity_ids=sensor_ids,
        selected_entity_id=sensor_id,
        selected_state_raw=raw_value,
        selected_value=selected_value,
        comparison_passed=comparison_passed,
        operator=condition.op,
        expected_value=condition.value,
        unit=condition.unit,
    )


def _resolve_condition_fact(controller: WateringController, condition: Condition, entity_store: EntityStore) -> PolicyFact:
    if condition.field == "moisture":
        return _resolve_moisture_fact(controller, condition, entity_store)

    return PolicyFact(
        field=condition.field,
        status="unsupported",
        message=f"Condition field '{condition.field}' is not wired in the backend yet.",
        operator=condition.op,
        expected_value=condition.value,
        unit=condition.unit,
    )


def evaluate_watering_policy(
    *,
    rule: Rule,
    controller: WateringController,
    entity_store: EntityStore,
    runtime_state: WateringRuntimeState | None,
    controller_paused: bool = False,
    now: datetime | None = None,
) -> WateringPolicyDecision:
    if now is None:
        evaluated_at = _utc_now()
    elif now.tzinfo is None:
        evaluated_at = now.replace(tzinfo=timezone.utc)
    else:
        evaluated_at = now.astimezone(timezone.utc)

    if not rule.enabled:
        return _decision(
            outcome="deny",
            reason_code="rule_disabled",
            message="Rule is disabled.",
            evaluated_at=evaluated_at,
        )

    if rule.paused:
        return _decision(
            outcome="deny",
            reason_code="rule_paused",
            message="Rule is paused.",
            evaluated_at=evaluated_at,
        )

    if rule.action.type != "watering.start":
        return _decision(
            outcome="unknown",
            reason_code="action_type_unsupported",
            message=f"Action '{rule.action.type}' is not supported by the watering policy.",
            evaluated_at=evaluated_at,
        )

    if rule.action.seconds <= 0:
        return _decision(
            outcome="unknown",
            reason_code="action_seconds_invalid",
            message="Action duration must be greater than 0 seconds.",
            evaluated_at=evaluated_at,
        )

    effective_seconds = min(rule.action.seconds, MAX_POLICY_WATERING_SECONDS)

    if runtime_state is not None and runtime_state.mode in (
        WateringMode.STARTING,
        WateringMode.ON,
        WateringMode.STOPPING,
    ):
        return _decision(
            outcome="deny",
            reason_code="runtime_busy",
            message=f"Controller is busy (mode={runtime_state.mode.value}).",
            evaluated_at=evaluated_at,
            effective_seconds=effective_seconds,
        )

    if controller_paused:
        return _decision(
            outcome="deny",
            reason_code="controller_paused",
            message="Controller is paused.",
            evaluated_at=evaluated_at,
            effective_seconds=effective_seconds,
        )

    if not rule.conditions:
        message = "No conditions configured. Policy allows the scheduled watering run."
        if effective_seconds != rule.action.seconds:
            message += f" Duration is capped to {effective_seconds} seconds by failsafe."
        return _decision(
            outcome="allow",
            reason_code="no_conditions_configured",
            message=message,
            evaluated_at=evaluated_at,
            effective_seconds=effective_seconds,
        )

    facts = tuple(_resolve_condition_fact(controller, condition, entity_store) for condition in rule.conditions)
    first_failed = next((fact for fact in facts if fact.comparison_passed is False), None)
    if first_failed is not None:
        return _decision(
            outcome="deny",
            reason_code="conditions_not_met",
            message=f"{first_failed.message} Condition is not met.",
            evaluated_at=evaluated_at,
            effective_seconds=effective_seconds,
            facts=facts,
        )

    first_unknown = next((fact for fact in facts if fact.status != "ok"), None)
    if first_unknown is not None:
        reason_code = {
            "missing": "condition_source_missing",
            "invalid": "condition_value_invalid",
            "unsupported": "condition_field_unsupported",
        }[first_unknown.status]
        return _decision(
            outcome="unknown",
            reason_code=reason_code,
            message=first_unknown.message,
            evaluated_at=evaluated_at,
            effective_seconds=effective_seconds,
            facts=facts,
        )

    message = "All configured conditions are met."
    if effective_seconds != rule.action.seconds:
        message += f" Duration is capped to {effective_seconds} seconds by failsafe."
    return _decision(
        outcome="allow",
        reason_code="conditions_met",
        message=message,
        evaluated_at=evaluated_at,
        effective_seconds=effective_seconds,
        facts=facts,
    )


def build_watering_rule_preview(
    *,
    rule: Rule,
    controller: WateringController,
    entity_store: EntityStore,
    runtime_state: WateringRuntimeState | None,
    controller_paused: bool = False,
    skip_next_pending: bool = False,
    now: datetime | None = None,
) -> WateringRulePreview:
    decision = evaluate_watering_policy(
        rule=rule,
        controller=controller,
        entity_store=entity_store,
        runtime_state=runtime_state,
        controller_paused=controller_paused,
        now=now,
    )
    if now is None:
        preview_now = decision.evaluated_at
    elif now.tzinfo is None:
        preview_now = now.replace(tzinfo=timezone.utc)
    else:
        preview_now = now

    time_effective_next_run_at = effective_next_run_at(
        rule=rule,
        raw_next_run_at=compute_rule_next_run_at(rule, now=preview_now),
        controller_paused=controller_paused,
        skip_next_pending=skip_next_pending,
        now=preview_now,
    )
    return WateringRulePreview(
        controller_id=controller.controller_id,
        summary=summarize_rule(rule, effective_seconds=decision.effective_seconds),
        next_run_at=time_effective_next_run_at,
        decision=decision,
    )
