from dataclasses import dataclass
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Request, status

from backend.policy.watering import PolicyFact, WateringPolicyDecision, WateringRulePreview, build_watering_rule_preview
from backend.rules_store.watering import Rule, RuleVersionConflictError
from backend.schemas.rules import (
    PolicyDecisionView,
    PolicyFactView,
    RulePreviewView,
    RuleUpsert,
    RuleView,
)
from backend.session_context import get_request_session_id

rules_router = APIRouter()


@dataclass(frozen=True)
class _RuleScope:
    entity_store: Any
    watering_controllers: Any
    rules_store: Any
    watering_runtime: Any
    watering_scheduler: Any

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


def _global_scope() -> _RuleScope:
    from backend.__main__ import entity_store, rules_store, watering_controllers, watering_runtime, watering_scheduler

    return _RuleScope(
        entity_store=entity_store,
        watering_controllers=watering_controllers,
        rules_store=rules_store,
        watering_runtime=watering_runtime,
        watering_scheduler=watering_scheduler,
    )


def _demo_scope(request: Request) -> _RuleScope:
    from backend.__main__ import demo_sessions

    session = demo_sessions.get(get_request_session_id(request))
    return _RuleScope(
        entity_store=session.entity_store,
        watering_controllers=session.watering_controllers,
        rules_store=session.rules_store,
        watering_runtime=session.watering_runtime,
        watering_scheduler=session.watering_scheduler,
    )


def _scope_for_listing(request: Request) -> _RuleScope:
    demo_scope = _demo_scope(request)
    if demo_scope.watering_controllers.list_all() or demo_scope.rules_store.list_all():
        return demo_scope
    return _global_scope()


def _scope_for_controller(request: Request, controller_id: str) -> _RuleScope:
    demo_scope = _demo_scope(request)
    if demo_scope.watering_controllers.get(controller_id) is not None:
        return demo_scope
    return _global_scope()


@rules_router.get(
    "/domains/watering/rules",
    response_model=list[RuleView]
)
async def get_all_rules(request: Request):
    scope = _scope_for_listing(request)
    return [_to_rule_view(rule) for rule in scope.rules_store.list_all()]


@rules_router.get(
    "/domains/watering/controllers/{controller_id}/rule",
    response_model=RuleView
)
async def get_controller_rule(controller_id: str, request: Request):
    scope = _scope_for_controller(request, controller_id)
    rule_by_controller_id = scope.rules_store.get(controller_id=controller_id)

    controller = scope.watering_controllers.get(controller_id)
    if not controller:
        raise HTTPException(status_code=404, detail=f"No controller found with id {controller_id}")

    if rule_by_controller_id is None:
        raise HTTPException(status_code=404, detail=f"No rule found for the controller with id {controller_id}")

    return _to_rule_view(rule_by_controller_id)

@rules_router.put(
    "/domains/watering/controllers/{controller_id}/rule",
    response_model=RuleView
)
async def upsert_controller_rule(controller_id: str, req: RuleUpsert, request: Request) -> RuleView:
    scope = _scope_for_controller(request, controller_id)

    controller = scope.watering_controllers.get(controller_id)
    if not controller:
        raise HTTPException(status_code=404, detail=f"No controller found with id {controller_id}")

    try:
        rule = scope.rules_store.upsert(
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

    stored = scope.rules_store.get(controller_id=rule.controller_id)
    if stored is None:
        raise HTTPException(status_code=500, detail="Rule upsert succeeded but stored rule is missing.")
    return _to_rule_view(stored)

@rules_router.delete(
    "/domains/watering/controllers/{controller_id}/rule",
    response_model=Dict[str, Any]
)
async def delete_controller_rule(controller_id: str, request: Request) -> Dict[str, Any]:
    scope = _scope_for_controller(request, controller_id)

    controller = scope.watering_controllers.get(controller_id)
    rule_by_controller_id = scope.rules_store.get(controller_id=controller_id)

    if not controller:
        raise HTTPException(status_code=404, detail=f"No controller found with id {controller_id}")

    if rule_by_controller_id is None:
        raise HTTPException(status_code=404, detail=f"No rule found for the controller with id {controller_id}")
    scope.rules_store.delete(controller_id)
    return {"deleted": True, "controller_id": controller_id}

@rules_router.post(
    "/domains/watering/controllers/{controller_id}/rule/preview",
    response_model=RulePreviewView,
)
async def preview_controller_rule(controller_id: str, req: RuleUpsert, request: Request) -> RulePreviewView:
    scope = _scope_for_controller(request, controller_id)

    controller = scope.watering_controllers.get(controller_id)
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
        entity_store=scope.entity_store,
        runtime_state=scope.watering_runtime.get_state(controller_id),
        controller_paused=scope.watering_scheduler.is_controller_paused(controller_id),
        skip_next_pending=scope.watering_scheduler.is_skip_next_pending(controller_id)
    )
    return _to_rule_preview_view(preview)
