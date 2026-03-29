import json
import hmac
import logging
from typing import List

from fastapi import WebSocket, WebSocketDisconnect, APIRouter

from backend.schemas.agents import AgentListResponse, Agent
from backend.schemas.ha_messages import EntitiesSnapshot, EntityStateUpdate
from backend.settings.config import settings

logger = logging.getLogger(__name__)
agent_router = APIRouter()
_BETA_KEY_HEADER = "x-semantic-home-beta-key"



@agent_router.websocket("/ws/agent/{agent_id}")
async def ws_agent(agent_id: str, ws: WebSocket) -> None:
    from backend.__main__ import entity_store, agent_hub, watering_runtime

    expected_key = settings.semantic_home_beta_key
    if expected_key:
        provided_key = ws.headers.get(_BETA_KEY_HEADER, "")
        if not hmac.compare_digest(provided_key, expected_key):
            await ws.close(code=4403, reason="Invalid beta key")
            return

    await ws.accept()

    # MVP: metadata via query params
    name = ws.query_params.get("name", "")
    description = ws.query_params.get("description", "")

    await agent_hub.register(agent_id, ws, name=name, description=description)

    try:
        while True:
            raw = await ws.receive_text()
            logger.info(f"Received message from Agent {agent_id}: {raw}")
            await agent_hub.touch(agent_id)
            msg = json.loads(raw)

            t = msg.get("type")

            if t == "entities_snapshot":
                snapshot = EntitiesSnapshot.model_validate(msg)
                for e in snapshot.entities:
                    entity_store.upsert(
                        e.entity_id,
                        e.state,
                        e.attributes,
                        source="agent",
                        source_id=agent_id,
                    )
                    await watering_runtime.observe_entity_state(e.entity_id, e.state)
                continue

            if t == "state_update":
                upd = EntityStateUpdate.model_validate(msg)
                e = upd.entity
                entity_store.upsert(
                    e.entity_id,
                    e.state,
                    e.attributes,
                    source="agent",
                    source_id=agent_id,
                )
                await watering_runtime.observe_entity_state(e.entity_id, e.state)
                continue

            if t == "pong":
                await ws.send_text(json.dumps({"type": "pong"}))

            await agent_hub.handle_incoming(msg)

    except WebSocketDisconnect:
        pass
    finally:
        await agent_hub.unregister(agent_id)

@agent_router.get("/agent/list_agents", response_model=AgentListResponse)
async def list_agents():
    from ...__main__ import agent_hub
    records = await agent_hub.list_agents()

    agents: List[Agent] = [
        Agent(
            id=r.agent_id,
            name=r.name,
            description=r.description,
            created_at=r.created_at,
            connected=r.connected,
            last_seen_at=r.last_seen_at,
        )
        for r in records
    ]
    return AgentListResponse(agents=agents)
