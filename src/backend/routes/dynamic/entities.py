from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from backend.entities_store.entities import EntityRecord
from backend.schemas.entities import EntityView, EntityActionRequest, EntityActionResponse
from backend.semantic.capabilities import infer_capabilities
from backend.session_context import get_request_session_id

entities_router = APIRouter()


def _resolve_entity_store(request: Request):
    from backend.__main__ import demo_sessions, entity_store

    session = demo_sessions.get(get_request_session_id(request))
    if session.has_demo_entities():
        return session.entity_store
    return entity_store


@entities_router.get("/domains/{domain}/entities", response_model=list[EntityView])
async def list_entities(domain: str, request: Request) -> List[EntityView]:
    entity_store = _resolve_entity_store(request)

    result: List[EntityView] = []
    for rec in entity_store.list_all():
        ent_domain = rec.entity_id.split(".", 1)[0]
        # Optional: Für MVP nur wateringspezifische Domains zeigen
        # z. B. bei domain=="watering" nur switch/valve/sensor
        if domain == "watering" and ent_domain not in ("switch", "valve", "sensor"):
            continue

        attrs: Dict[str, Any] = rec.attributes or {}
        result.append(
            EntityView(
                entity_id=rec.entity_id,
                domain=ent_domain,
                name=attrs.get("friendly_name"),
                state=rec.state,
                attributes=attrs,
                capabilities=infer_capabilities(rec.entity_id, attrs),
            )
        )
    return result


@entities_router.get("/domains/{domain}/entities/{entity_id:path}", response_model=EntityView)
async def get_entity(domain: str, entity_id: str, request: Request) -> EntityView:
    entity_store = _resolve_entity_store(request)

    rec: EntityRecord = entity_store.get(entity_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Unknown entity_id")

    ent_domain: str = entity_id.split(".", 1)[0]
    attrs: Dict[str, Any] = rec.attributes or {}
    return EntityView(
        entity_id=rec.entity_id,
        domain=ent_domain,
        name=attrs.get("friendly_name"),
        state=rec.state,
        attributes=attrs,
        capabilities=infer_capabilities(rec.entity_id, attrs),
    )

@entities_router.post("/domains/{domain}/entities/actions", response_model=EntityActionResponse)
async def execute_entity_action(domain: str, req: EntityActionRequest) -> EntityActionResponse:
    from ...__main__ import agent_hub
    # MVP: mapping Action -> HA service call (Domain+Service+Data)
    ent_domain = req.entity_id.split(".", 1)[0]

    # nur MVP-allowlist
    if ent_domain not in ("switch", "valve"):
        raise HTTPException(status_code=400, detail="Entity is not controllable in MVP")

    if req.action == "turn_on":
        ha_call = {"type": "ha_call_service", "domain": ent_domain, "service": "turn_on",
                   "data": {"entity_id": req.entity_id}}
    elif req.action == "turn_off":
        ha_call = {"type": "ha_call_service", "domain": ent_domain, "service": "turn_off",
                   "data": {"entity_id": req.entity_id}}
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {req.action}")

    # Agent auswählen: MVP-hart (z. B. "home1"), später über site/home binding
    agent_id = "home1"

    from ...__main__ import agent_hub
    cmd = await agent_hub.send_command_and_wait_ack(
        agent_id=agent_id,
        name="ha.call_service",
        args=ha_call,
        timeout_s=5.0,
    )

    if not cmd.ok:
        return EntityActionResponse(ok=False, error=cmd.error)

    return EntityActionResponse(ok=True, applied={"entity_id": req.entity_id, "action": req.action})
