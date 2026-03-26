from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class HAEntityState(BaseModel):
    entity_id: str
    state: str
    attributes: Dict[str, Any] = Field(default_factory=dict)

class EntitiesSnapshot(BaseModel):
    type: Literal["entities_snapshot"]
    entities: List[HAEntityState]

class EntityStateUpdate(BaseModel):
    type: Literal["state_update"]
    entity: HAEntityState

class Hello(BaseModel):
    type: Literal["hello"]
    agent_id: Optional[str] = None


AgentInboundMessageType = Literal["hello", "entities_snapshot", "state_update", "ack"]