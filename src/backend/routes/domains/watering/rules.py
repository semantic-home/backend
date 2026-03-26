from typing import Dict, Any

from fastapi import APIRouter, HTTPException, status

from backend.policy.watering import PolicyFact, WateringPolicyDecision, WateringRulePreview, build_watering_rule_preview
from backend.rules_store.watering import Rule, RuleVersionConflictError
from backend.schemas.rules import (
    PolicyDecisionView,
    PolicyFactView,
    RulePreviewView,
    RuleUpsert,
    RuleView,
)

rules_router = APIRouter()

def _to_rule_view(rule: Rule) -> RuleView:
    return RuleView(
        controller_id=rule.controller_id,
        version=rule.version,
        enabled=rule.enabled,
        paused=rule.paused,
        schedule=rule.schedule,
        conditions=rule.conditions,
        action=rule.action,
        created_at=rule.created_at,
        updated_at=rule.updated_at
    )


def _to_policy_fact_view(fact: PolicyFact) -> PolicyFactView:
    return PolicyFactView(
        field=fact.field,
        status=fact.status,
        message=fact.message,
        source_entity_ids=list(fact.source_entity_ids),
        selected_entity_id=fact.selected_entity_id,
        selected_state_raw=fact.selected_state_raw,
        selected_value=fact.selected_value,
        comparison_passed=fact.comparison_passed,
        operator=fact.operator,
        expected_value=fact.expected_value,
        unit=fact.unit,
    )


def _to_policy_decision_view(decision: WateringPolicyDecision) -> PolicyDecisionView:
    return PolicyDecisionView(
        outcome=decision.outcome,
        reason_code=decision.reason_code,
        message=decision.message,
        evaluated_at=decision.evaluated_at,
        effective_seconds=decision.effective_seconds,
        facts=[_to_policy_fact_view(fact) for fact in decision.facts],
    )


def _to_rule_preview_view(preview: WateringRulePreview) -> RulePreviewView:
    return RulePreviewView(
        controller_id=preview.controller_id,
        summary=preview.summary,
        next_run_at=preview.next_run_at,
        decision=_to_policy_decision_view(preview.decision),
    )


@rules_router.get(
    "/domains/watering/rules",
    response_model=list[RuleView]
)
async def get_all_rules():
    from backend.__main__ import rules_store
    return [_to_rule_view(rule) for rule in rules_store.list_all()]


@rules_router.get(
    "/domains/watering/controllers/{controller_id}/rule",
    response_model=RuleView
)
async def get_controller_rule(controller_id: str):
    from backend.__main__ import rules_store, watering_controllers
    rule_by_controller_id = rules_store.get(controller_id=controller_id)

    controller = watering_controllers.get(controller_id)
    if not controller:
        raise HTTPException(status_code=404, detail=f"No controller found with id {controller_id}")

    if rule_by_controller_id is None:
        raise HTTPException(status_code=404, detail=f"No rule found for the controller with id {controller_id}")

    return _to_rule_view(rule_by_controller_id)

@rules_router.put(
    "/domains/watering/controllers/{controller_id}/rule",
    response_model=RuleView
)
async def upsert_controller_rule(controller_id: str, req: RuleUpsert) -> RuleView:
    from backend.__main__ import rules_store, watering_controllers

    controller = watering_controllers.get(controller_id)
    if not controller:
        raise HTTPException(status_code=404, detail=f"No controller found with id {controller_id}")

    try:
        rule = rules_store.upsert(
            Rule(
                controller_id=controller_id,
                enabled=req.enabled,
                paused=req.paused,
                schedule=req.schedule,
                conditions=req.conditions,
                action=req.action,
            ),
            expected_version=req.expected_version,
        )
    except RuleVersionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    stored = rules_store.get(controller_id=rule.controller_id)
    if stored is None:
        raise HTTPException(status_code=500, detail="Rule upsert succeeded but stored rule is missing.")
    return _to_rule_view(stored)

@rules_router.delete(
    "/domains/watering/controllers/{controller_id}/rule",
    response_model=Dict[str, Any]
)
async def delete_controller_rule(controller_id: str) -> Dict[str, Any]:
    from backend.__main__ import rules_store, watering_controllers

    controller = watering_controllers.get(controller_id)
    rule_by_controller_id = rules_store.get(controller_id=controller_id)

    if not controller:
        raise HTTPException(status_code=404, detail=f"No controller found with id {controller_id}")

    if rule_by_controller_id is None:
        raise HTTPException(status_code=404, detail=f"No rule found for the controller with id {controller_id}")
    rules_store.delete(controller_id)
    return {"deleted": True, "controller_id": controller_id}

@rules_router.post(
    "/domains/watering/controllers/{controller_id}/rule/preview",
    response_model=RulePreviewView,
)
async def preview_controller_rule(controller_id: str, req: RuleUpsert) -> RulePreviewView:
    from backend.__main__ import entity_store, watering_controllers, watering_runtime, watering_scheduler

    controller = watering_controllers.get(controller_id)
    if not controller:
        raise HTTPException(status_code=404, detail=f"No controller found with id {controller_id}")

    preview = build_watering_rule_preview(
        rule=Rule(
            controller_id=controller_id,
            enabled=req.enabled,
            paused=req.paused,
            schedule=req.schedule,
            conditions=req.conditions,
            action=req.action,
        ),
        controller=controller,
        entity_store=entity_store,
        runtime_state=watering_runtime.get_state(controller_id),
        controller_paused=watering_scheduler.is_controller_paused(controller_id),
        skip_next_pending=watering_scheduler.is_skip_next_pending(controller_id)
    )
    return _to_rule_preview_view(preview)
