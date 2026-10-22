from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from psycopg.types.json import Jsonb

from backend.platform.database import token_hash
from backend.schemas.ha_messages import EntitiesSnapshot, EntityStateUpdate

logger = logging.getLogger(__name__)
agent_router = APIRouter()


@agent_router.websocket("/ws/agent/{agent_id}")
async def ws_agent(agent_id: str, ws: WebSocket):
    db, hub = ws.app.state.db, ws.app.state.agent_hub
    provided = ws.headers.get("authorization", "").removeprefix("Bearer ")
    agent = db.one("SELECT id,home_id,name FROM agents WHERE id=%s AND token_hash=%s AND NOT revoked",
                   (agent_id, token_hash(provided)))
    if not provided or not agent:
        await ws.close(code=4403, reason="Invalid agent credential")
        return
    await ws.accept()
    await hub.register(agent_id, ws, name=agent["name"])
    sync_task = None
    try:
        while True:
            raw = await ws.receive_text()
            if not hub.is_current(agent_id, ws):
                break
            if not db.agent(agent["home_id"], agent_id):
                await ws.close(code=4403)
                break
            msg = json.loads(raw)
            kind = msg.get("type")
            await hub.touch(agent_id)
            if kind == "metadata":
                metadata = {key: msg[key] for key in ("timezone", "ha_version", "stub") if key in msg}
                db.execute("UPDATE agents SET metadata=%s WHERE home_id=%s AND id=%s", (Jsonb(metadata), agent["home_id"], agent_id))
            elif kind in {"entities_snapshot", "state_update"}:
                entities = EntitiesSnapshot.model_validate(msg).entities if kind == "entities_snapshot" else [EntityStateUpdate.model_validate(msg).entity]
                previous = {}
                if kind == "state_update":
                    previous = {r["entity_id"]: r for r in db.all("SELECT id,entity_id,state FROM devices WHERE home_id=%s AND agent_id=%s AND entity_id=%s",
                                                                 (agent["home_id"], agent_id, entities[0].entity_id))}
                await asyncio.to_thread(db.ingest, agent["home_id"], agent_id, entities, snapshot=kind == "entities_snapshot")
                scope = ws.app.state.home_scopes.scopes.get(agent["home_id"])
                watched = {eid for c in (scope.watering_controllers.list_all() if scope else []) if c.agent_id == agent_id
                           for eid in c.actuator_entity_ids + (c.moisture_sensor_entity_ids or [])}
                for entity in entities:
                    if entity.entity_id in watched:
                        record = scope.entity_store.get(entity.entity_id)
                        if record and record.source_id == agent_id:
                            await scope.watering_runtime.observe_entity_state(entity.entity_id, entity.state)
                    prior = previous.get(entity.entity_id)
                    if prior and prior["state"] != entity.state:
                        for rule in db.all("SELECT id FROM automations WHERE home_id=%s AND agent_id=%s AND deployed_spec->'targets' ? %s AND status<>'deleted'",
                                           (agent["home_id"], agent_id, prior["id"])):
                            db.event(agent["home_id"], rule["id"], "device_state", {"entity_id": entity.entity_id, "state": entity.state})
                if kind == "entities_snapshot" and (sync_task is None or sync_task.done()):
                    sync_task = asyncio.create_task(ws.app.state.automations.sync_agent(agent["home_id"], agent_id))
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
            elif kind == "ack":
                await hub.handle_incoming(msg, agent_id=agent_id, ws=ws)
            elif kind == "automation_event":
                data = msg.get("data", {})
                if isinstance(data, dict):
                    rule = db.rule(agent["home_id"], data.get("automation_id", ""))
                    if rule and rule["agent_id"] == agent_id:
                        detail = {key: str(data.get(key, ""))[:200] for key in ("entity_id", "desired", "reason")}
                        db.event(agent["home_id"], rule["id"], "action_requested", detail)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except (ValueError, ValidationError):
        logger.warning("Rejected invalid message from connection %s", agent_id)
        await ws.close(code=4400, reason="Invalid agent message")
    finally:
        if sync_task:
            sync_task.cancel()
            await asyncio.gather(sync_task, return_exceptions=True)
        if hub.is_current(agent_id, ws):
            db.execute("UPDATE devices SET available=false WHERE home_id=%s AND agent_id=%s", (agent["home_id"], agent_id))
        await hub.unregister(agent_id, ws=ws)


@agent_router.get("/agent/list_agents")
async def list_agents(request: Request):
    own_ids = {a["id"] for a in request.app.state.db.all("SELECT id FROM agents WHERE home_id=%s AND NOT revoked", (request.state.home["id"],))}
    records = await request.app.state.agent_hub.list_agents()
    return {"agents": [{"id": r.agent_id, "name": r.name, "description": r.description,
                        "created_at": r.created_at, "connected": r.connected, "last_seen_at": r.last_seen_at}
                       for r in records if r.agent_id in own_ids]}
