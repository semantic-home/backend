from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from backend.entities_store.entities import EntityRecord
from backend.schemas.entities import EntityView, EntityActionRequest, EntityActionResponse
from backend.semantic.capabilities import infer_capabilities

entities_router = APIRouter()

@entities_router.get("/domains/{domain}/entities", response_model=list[EntityView])
async def list_entities(domain: str) -> List[EntityView]:
    from ...__main__ import entity_store

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
async def get_entity(domain: str, entity_id: str) -> EntityView:
    from ...__main__ import entity_store

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
