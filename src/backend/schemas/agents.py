from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel

class Agent(BaseModel):
    id: str
    name: str
    description: str
    created_at: datetime
    connected: bool
    last_seen_at: Optional[datetime] = None



class AgentListResponse(BaseModel):
    agents: List[Agent]
