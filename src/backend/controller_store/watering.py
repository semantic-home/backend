from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from backend.schemas.zone import Zone

@dataclass
class WateringController:
    controller_id: str
    display_name: str
    actuator_entity_ids: list[str]
    agent_id: str
    requires_agent: bool = True
    moisture_sensor_entity_ids: list[str] | None = None
    moisture_start_below: float | None = None
    zone: Zone | None = None
    plant_ids: list[str] | None = None

class WateringControllerStore:
    def __init__(self) -> None:
        self._controllers: Dict[str, WateringController] = {}

    def clear(self) -> None:
        self._controllers.clear()

    def upsert(self, c: WateringController) -> None:
        self._controllers[c.controller_id] = c

    def get(self, controller_id: str) -> Optional[WateringController]:
        return self._controllers.get(controller_id)

    def list_all(self) -> list[WateringController]:
        return list(self._controllers.values())

    def delete(self, controller_id: str) -> None:
        self._controllers.pop(controller_id, None)
