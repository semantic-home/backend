from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend.controller_store.watering import WateringController
from backend.session_context import get_request_session_id
from backend.schemas.controller import (
    WateringControllerCreate,
    WateringControllerUpdate,
    WateringNextView,
    WateringControllerView,
    WateringStartRequest,
    WateringWhyView,
)

watering_router = APIRouter()


@dataclass(frozen=True)
class _StoreScope:
    entity_store: Any
    watering_controllers: Any
    rules_store: Any
    watering_runtime: Any
    watering_scheduler: Any

_PLANT_ICON_BY_KEYWORD: dict[str, str] = {
    "monstera": "leaf",
    "ficus": "trees",
    "palm": "trees",
    "fern": "leaf",
    "calathea": "leaf",
    "pilea": "leaf",
    "epipremnum": "leaf",
    "chlorophytum": "leaf",
    "nephrolepis": "leaf",
    "orchid": "flower",
    "rose": "flower",
    "geranium": "flower",
    "pelargonium": "flower",
    "tomato": "sprout",
    "chili": "sprout",
    "pepper": "sprout",
    "ocimum": "sprout",
    "basil": "sprout",
    "herb": "sprout",
    "cactus": "sprout",
    "succulent": "droplets",
    "echeveria": "droplets",
    "crassula": "droplets",
    "zamioculcas": "trees",
    "dracaena": "trees",
    "sansevieria": "trees",
}


def _normalize_plant_ids(plant_ids: list[str]) -> list[str]:
    cleaned = [plant_id.strip() for plant_id in plant_ids if plant_id.strip()]
    return list(dict.fromkeys(cleaned))


def _icon_key_for_plant_id(plant_id: str) -> str:
    lowered = plant_id.lower()
    for keyword, icon_key in _PLANT_ICON_BY_KEYWORD.items():
        if keyword in lowered:
            return icon_key
    return "leaf"


def _to_controller_view(controller: WateringController) -> WateringControllerView:
    plant_ids = list(controller.plant_ids or [])
    icon_keys = [_icon_key_for_plant_id(plant_id) for plant_id in plant_ids]
    return WateringControllerView(
        controller_id=controller.controller_id,
        display_name=controller.display_name,
        actuator_entity_ids=controller.actuator_entity_ids,
        agent_id=controller.agent_id,
        requires_agent=controller.requires_agent,
        moisture_sensor_entity_ids=list(controller.moisture_sensor_entity_ids or []),
        moisture_start_below=controller.moisture_start_below,
        zone=controller.zone,
        plant_ids=plant_ids,
        icon_keys=icon_keys,
        plant_count=len(plant_ids),
    )


def _normalize_controller_entities(
    actuator_entity_ids: list[str],
    moisture_sensor_entity_ids: list[str],
    plant_ids: list[str],
    *,
    legacy_actuator_entity_id: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    normalized_actuator_ids = list(actuator_entity_ids)
    if legacy_actuator_entity_id and legacy_actuator_entity_id not in normalized_actuator_ids:
        normalized_actuator_ids.append(legacy_actuator_entity_id)
    normalized_actuator_ids = list(dict.fromkeys(normalized_actuator_ids))
    normalized_moisture_sensor_ids = list(dict.fromkeys(moisture_sensor_entity_ids))
    normalized_plant_ids = _normalize_plant_ids(plant_ids)
    return normalized_actuator_ids, normalized_moisture_sensor_ids, normalized_plant_ids


def _ensure_entities_exist(
    *,
    entity_store: Any,
    actuator_ids: list[str],
    moisture_sensor_ids: list[str],
) -> None:
    missing: list[str] = []
    for entity_id in actuator_ids:
        if entity_store.get(entity_id) is None:
            missing.append(entity_id)
    for entity_id in moisture_sensor_ids:
        if entity_store.get(entity_id) is None:
            missing.append(entity_id)

    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_entities",
                "missing": missing,
                "hint": "Connect an agent and send an entities_snapshot first.",
            },
        )


def _ensure_no_shared_actuator_conflicts(
    *,
    controller_store: Any,
    requested_targets: frozenset[str],
    exclude_controller_id: str | None = None,
) -> None:
    for existing in controller_store.list_all():
        if existing.controller_id == exclude_controller_id:
            continue
        overlap = requested_targets.intersection(existing.actuator_entity_ids)
        if overlap:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "shared_actuator_conflict",
                    "controller_id": existing.controller_id,
                    "shared_actuator_entity_ids": sorted(overlap),
                },
            )


def _global_scope() -> _StoreScope:
    from backend.__main__ import entity_store, rules_store, watering_controllers, watering_runtime, watering_scheduler

    return _StoreScope(
        entity_store=entity_store,
        watering_controllers=watering_controllers,
        rules_store=rules_store,
        watering_runtime=watering_runtime,
        watering_scheduler=watering_scheduler,
    )


def _demo_scope(request: Request) -> _StoreScope:
    from backend.__main__ import demo_sessions

    session = demo_sessions.get(get_request_session_id(request))
    return _StoreScope(
        entity_store=session.entity_store,
        watering_controllers=session.watering_controllers,
        rules_store=session.rules_store,
        watering_runtime=session.watering_runtime,
        watering_scheduler=session.watering_scheduler,
    )


def _scope_for_create(request: Request, *, requires_agent: bool) -> _StoreScope:
    if requires_agent:
        return _global_scope()
    return _demo_scope(request)


def _scope_for_listing(request: Request) -> _StoreScope:
    demo_scope = _demo_scope(request)
    if demo_scope.watering_controllers.list_all():
        return demo_scope
    return _global_scope()


def _scope_for_controller(request: Request, controller_id: str) -> _StoreScope:
    demo_scope = _demo_scope(request)
    if demo_scope.watering_controllers.get(controller_id) is not None:
        return demo_scope
    return _global_scope()

@watering_router.post("/domains/watering/controllers", response_model=WateringControllerView)
async def create_controller(request: Request, req: WateringControllerCreate) -> WateringControllerView:
    scope = _scope_for_create(request, requires_agent=req.requires_agent)

    actuator_ids, moisture_sensor_ids, plant_ids = _normalize_controller_entities(
        req.actuator_entity_ids,
        req.moisture_sensor_entity_ids,
        req.plant_ids,
        legacy_actuator_entity_id=req.actuator_entity_id,
    )
    _ensure_entities_exist(
        entity_store=scope.entity_store,
        actuator_ids=actuator_ids,
        moisture_sensor_ids=moisture_sensor_ids,
    )
    _ensure_no_shared_actuator_conflicts(
        controller_store=scope.watering_controllers,
        requested_targets=frozenset(actuator_ids),
        exclude_controller_id=req.controller_id,
    )

    c = WateringController(
        controller_id=req.controller_id,
        display_name=req.display_name,
        actuator_entity_ids=actuator_ids,
        agent_id=req.agent_id,
        requires_agent=req.requires_agent,
        moisture_sensor_entity_ids=moisture_sensor_ids,
        moisture_start_below=req.moisture_start_below,
        zone=req.zone,
        plant_ids=plant_ids,
    )
    scope.watering_controllers.upsert(c)
    return _to_controller_view(c)


@watering_router.put("/domains/watering/controllers/{controller_id}", response_model=WateringControllerView)
async def update_controller(request: Request, controller_id: str, req: WateringControllerUpdate) -> WateringControllerView:
    scope = _scope_for_controller(request, controller_id)

    existing = scope.watering_controllers.get(controller_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    runtime_state = scope.watering_runtime.get_state(controller_id)
    if runtime_state is not None and runtime_state.mode.value not in {"OFF", "FAILED"}:
        raise HTTPException(status_code=409, detail="Controller is busy and cannot be edited right now.")

    actuator_ids, moisture_sensor_ids, plant_ids = _normalize_controller_entities(
        req.actuator_entity_ids,
        req.moisture_sensor_entity_ids,
        req.plant_ids,
    )
    _ensure_entities_exist(
        entity_store=scope.entity_store,
        actuator_ids=actuator_ids,
        moisture_sensor_ids=moisture_sensor_ids,
    )
    _ensure_no_shared_actuator_conflicts(
        controller_store=scope.watering_controllers,
        requested_targets=frozenset(actuator_ids),
        exclude_controller_id=controller_id,
    )

    updated = WateringController(
        controller_id=controller_id,
        display_name=req.display_name,
        actuator_entity_ids=actuator_ids,
        agent_id=req.agent_id,
        requires_agent=req.requires_agent,
        moisture_sensor_entity_ids=moisture_sensor_ids,
        moisture_start_below=req.moisture_start_below,
        zone=req.zone,
        plant_ids=plant_ids,
    )
    scope.watering_controllers.upsert(updated)
    return _to_controller_view(updated)


@watering_router.delete("/domains/watering/controllers/{controller_id}", response_model=dict[str, Any])
async def delete_controller(request: Request, controller_id: str) -> dict[str, Any]:
    scope = _scope_for_controller(request, controller_id)

    existing = scope.watering_controllers.get(controller_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    await scope.watering_runtime.delete(controller_id)
    scope.rules_store.delete(controller_id)
    scope.watering_scheduler.delete(controller_id)
    scope.watering_controllers.delete(controller_id)
    return {"deleted": True, "controller_id": controller_id}

@watering_router.get("/domains/watering/controllers", response_model=list[WateringControllerView])
async def list_controllers(request: Request) -> list[WateringControllerView]:
    scope = _scope_for_listing(request)
    return [_to_controller_view(c) for c in scope.watering_controllers.list_all()]

@watering_router.get("/domains/watering/zones/{zone_id}/controllers", response_model=list[WateringControllerView])
async def list_zone_controllers(zone_id: str, request: Request) -> list[WateringControllerView]:
    scope = _scope_for_listing(request)
    return [
        _to_controller_view(c)
        for c in scope.watering_controllers.list_all()
        if c.zone is not None and c.zone.zone_id == zone_id
    ]

@watering_router.get(
    "/domains/watering/zones/{zone_id}/controllers/{controller_id}",
    response_model=WateringControllerView
)
async def get_controller_per_zone(zone_id: str, controller_id: str, request: Request) -> WateringControllerView:
    scope = _scope_for_controller(request, controller_id)
    controllers = [
        c
        for c in scope.watering_controllers.list_all()
        if c.zone is not None and c.zone.zone_id == zone_id and c.controller_id == controller_id
    ]

    if not controllers:
        raise HTTPException(status_code=404, detail=f"No controller found under {zone_id} with the id {controller_id}")
    if len(controllers) > 1:
        raise HTTPException(status_code=409, detail="Expected exactly one controller, but found multiple.")

    return _to_controller_view(controllers[0])

@watering_router.get("/domains/watering/controllers/{controller_id}/why", response_model=WateringWhyView)
async def why_controller(controller_id: str, request: Request) -> WateringWhyView:
    from backend.semantic.watering_why import compute_watering_why

    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    why = compute_watering_why(
        c,
        scope.entity_store,
        rule=scope.rules_store.get(controller_id),
        runtime_state=scope.watering_runtime.get_state(controller_id),
        controller_paused=scope.watering_scheduler.is_controller_paused(controller_id),
    )
    return WateringWhyView(
        controller_id=why.controller_id,
        decision=why.decision,  # type: ignore[arg-type]
        reason_code=why.reason_code,
        message=why.message,
        at=why.at,
        moisture_sensor_entity_ids=why.moisture_sensor_entity_ids,
        moisture_state_raw=why.moisture_state_raw,
        moisture_value=why.moisture_value,
        moisture_start_below=why.moisture_start_below,
    )


@watering_router.get("/domains/watering/controllers/{controller_id}/next", response_model=WateringNextView)
async def next_controller_run(controller_id: str, request: Request) -> WateringNextView:
    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    next_view = scope.watering_scheduler.get_state_view(controller_id)
    return WateringNextView(**next_view)


@watering_router.post("/domains/watering/controllers/{controller_id}/pause", response_model=WateringNextView)
async def pause_controller_schedule(controller_id: str, request: Request) -> WateringNextView:
    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    next_view = scope.watering_scheduler.set_paused(controller_id, True)
    return WateringNextView(**next_view)


@watering_router.post("/domains/watering/controllers/{controller_id}/resume", response_model=WateringNextView)
async def resume_controller_schedule(controller_id: str, request: Request) -> WateringNextView:
    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    next_view = scope.watering_scheduler.set_paused(controller_id, False)
    return WateringNextView(**next_view)


@watering_router.post("/domains/watering/controllers/{controller_id}/skip_next", response_model=WateringNextView)
async def skip_next_controller_run(controller_id: str, request: Request) -> WateringNextView:
    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    next_view = scope.watering_scheduler.skip_next(controller_id)
    if next_view is None:
        raise HTTPException(status_code=409, detail="No upcoming scheduled run to skip.")

    return WateringNextView(**next_view)

@watering_router.post("/domains/watering/controllers/{controller_id}/start")
async def start_controller(controller_id: str, req: WateringStartRequest, request: Request) -> dict:
    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    try:
        run_id = await scope.watering_runtime.start(c, seconds=req.seconds)
    except RuntimeError as exc:
        msg = str(exc)
        if "busy" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc

    return {
        "queued": True,
        "controller_id": controller_id,
        "seconds": req.seconds,
        "run_id": run_id,
        "status": "STARTING",
    }


@watering_router.post("/domains/watering/controllers/{controller_id}/stop")
async def stop_controller(controller_id: str, request: Request) -> dict:
    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    try:
        run_id = await scope.watering_runtime.stop(controller_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if run_id is None:
        return {"accepted": False, "controller_id": controller_id, "message": "Controller is already OFF."}

    return {"accepted": True, "controller_id": controller_id, "run_id": run_id}


@watering_router.get("/domains/watering/controllers/{controller_id}/state")
async def controller_runtime_state(controller_id: str, request: Request) -> dict:
    scope = _scope_for_controller(request, controller_id)

    c = scope.watering_controllers.get(controller_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Unknown controller_id")

    state = scope.watering_runtime.get_state_view(controller_id)
    state["display_name"] = c.display_name
    return state
