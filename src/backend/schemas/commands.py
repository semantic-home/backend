from __future__ import annotations

from typing import Any, Dict, Optional, Literal
from pydantic import BaseModel, Field


class CommandRequest(BaseModel):
    agent_id: str
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = 3.0


class CommandResponse(BaseModel):
    id: str
    ok: bool
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None




