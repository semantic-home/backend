from typing import Literal, Optional, Dict, Any

from pydantic import BaseModel, Field

DomainKey = Literal["watering", "lighting", "power", "surveillance"]

class EntityView(BaseModel):
    entity_id: str                       # "switch.garden_pump"
    domain: str                          # "switch" (Teil vor dem Punkt)
    name: Optional[str] = None           # friendly_name
    state: str                           # HA state as string (e.g. "on", "off", "62")
    attributes: Dict[str, Any] = Field(default_factory=dict)

    capabilities: Dict[str, Any] = Field(default_factory=dict)
    # Beispiel: {"actions": ["turn_on","turn_off"]} oder {"read_only": true, "unit": "%"}

class EntityActionRequest(BaseModel):
    entity_id: str
    action: str                          # e.g. "turn_on" | "turn_off" | "set_level" ...
    params: Dict[str, Any] = Field(default_factory=dict)

class EntityActionResponse(BaseModel):
    ok: bool
    error: Optional[str] = None
    # Optional: zurückgeben, was man "intended" hat
    applied: Optional[Dict[str, Any]] = None
