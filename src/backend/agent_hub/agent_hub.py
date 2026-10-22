from __future__ import annotations

import asyncio
import json
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import WebSocket, HTTPException

from ..schemas.commands import CommandResponse

logger = logging.getLogger(__name__)

@dataclass
class AgentRecord:
    agent_id: str
    name: str
    description: str
    created_at: datetime
    connected: bool = False
    last_seen_at: Optional[datetime] = None


class AgentHub:
    def __init__(self) -> None:
        self._agents: Dict[str, WebSocket] = {}
        self._agent_records: Dict[str, AgentRecord] = {}
        self._ack_waiters: dict[str, tuple[str, WebSocket, asyncio.Future]] = {}
        self._lock = asyncio.Lock()

    async def register(
            self,
            agent_id: str,
            ws: WebSocket, *,
            name: str = "",
            description: str = ""
    ) -> None:
        now = datetime.now(timezone.utc)
        previous = self._agents.get(agent_id)
        if previous and previous is not ws:
            await self.unregister(agent_id, ws=previous)
            await previous.close(code=4001, reason="Connection replaced")
        async with self._lock:
            self._agents[agent_id] = ws

            rec = self._agent_records.get(agent_id)
            if rec is None:
                rec = AgentRecord(
                    agent_id=agent_id,
                    name=name or agent_id,
                    description=description or "",
                    created_at=now,
                    connected=True,
                    last_seen_at=now,
                )
                self._agent_records[agent_id] = rec
                logger.info(f"Registered {agent_id}.")
            else:
                # keep created_at stable; update meta if provided
                if name:
                    rec.name = name
                if description:
                    rec.description = description
                rec.connected = True
                rec.last_seen_at = now
                logger.info(f"Updated {agent_id} metadata with {name=}, {description=}")


    async def unregister(self, agent_id: str, *, ws: WebSocket | None = None) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            if ws is not None and self._agents.get(agent_id) is not ws:
                return
            self._agents.pop(agent_id, None)
            rec = self._agent_records.get(agent_id)
            if rec is not None:
                rec.connected = False
                rec.last_seen_at = now
            self._agent_records.pop(agent_id, None)
            logger.info(f"Unregistered {agent_id}.")
            for target, connection, future in list(self._ack_waiters.values()):
                if target == agent_id and not future.done():
                    future.set_result({"ok": False, "error": "Agent disconnected"})

    async def disconnect(self, agent_id):
        ws = self._agents.get(agent_id)
        if ws:
            await self.unregister(agent_id, ws=ws)
            await ws.close(code=4403, reason="Connection revoked")

    async def shutdown(self):
        for agent_id in list(self._agents):
            await self.disconnect(agent_id)

    def is_current(self, agent_id, ws):
        return self._agents.get(agent_id) is ws

    def is_connected(self, agent_id):
        return agent_id in self._agents

    async def list_agents(self) -> List[AgentRecord]:
        async with self._lock:
            return list(self._agent_records.values())

    async def send_command_and_wait_ack(
            self,
            agent_id: str,
            name: str,
            args: Dict[str, Any],
            timeout_s: float
    ) -> CommandResponse:
        async with self._lock:
            logger.debug("Entered lock.")
            ws = self._agents.get(agent_id)
        if ws is None:
            raise HTTPException(status_code=404, detail=f"Agent not connected: {agent_id}")
        if sum(1 for target, _, _ in self._ack_waiters.values() if target == agent_id) >= 32:
            raise HTTPException(429, "Too many pending commands for this connection")
        logger.info(f"Executing command for {agent_id}")

        cmd_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_event_loop().create_future()

        self._ack_waiters[cmd_id] = (agent_id, ws, fut)
        try:
            payload = {"type": "command", "id": cmd_id, "name": name, "args": args,
                       "expires_at": datetime.now(timezone.utc).timestamp() + timeout_s}
            await ws.send_text(json.dumps(payload))

            try:
                ack = await asyncio.wait_for(fut, timeout=timeout_s)
            except asyncio.TimeoutError:
                return CommandResponse(id=cmd_id, ok=False, error="ACK timeout")

            if ack.get("ok") is True:
                return CommandResponse(id=cmd_id, ok=True, result=ack.get("result"))
            return CommandResponse(id=cmd_id, ok=False, error=ack.get("error", "Unknown error"))
        except (RuntimeError, OSError):
            return CommandResponse(id=cmd_id, ok=False, error="Agent connection failed")
        finally:
            self._ack_waiters.pop(cmd_id, None)

    async def touch(self, agent_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._lock:
            rec = self._agent_records.get(agent_id)
            if rec is not None:
                rec.last_seen_at = now
                logger.debug("Agent %s last seen at %s", agent_id, now)

    async def handle_incoming(self, msg: Dict[str, Any], *, agent_id: str, ws: WebSocket) -> None:
        if msg.get("type") == "ack":
            cmd_id = msg.get("id")
            waiter = self._ack_waiters.get(cmd_id)
            if not waiter:
                return
            target, connection, fut = waiter
            if target == agent_id and connection is ws and self.is_current(agent_id, ws) and not fut.done():
                fut.set_result(msg)
