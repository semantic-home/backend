from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.schemas.zone import Zone

Decision = Literal["start", "skip", "unknown"]


class GenericControllerView(BaseModel):
    controller_id: str
    display_name: str
    agent_id: str
    zone: Zone | None = None
    plant_ids: list[str] = Field(default_factory=list)
    icon_keys: list[str] = Field(default_factory=list)
    plant_count: int = 0


class GenericWhyView(BaseModel):
    controller_id: str
    decision: Decision
    reason_code: str
    message: str
    at: datetime
