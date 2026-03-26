from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.schemas.zone import Zone


class WateringControllerCreate(BaseModel):
    controller_id: str = Field(description="Stable ID, e.g. balcony, garden, tomatoes")
    display_name: str

    actuator_entity_ids: list[str] = Field(
        default_factory=list,
        description="HA entities to switch ON/OFF, e.g. ['switch.garden_pump','valve.zone_1']",
    )
    actuator_entity_id: str | None = Field(
        default=None,
        description="(legacy) Single HA entity to switch ON/OFF, e.g. switch.garden_pump",
    )

    moisture_sensor_entity_ids: list[str] = Field(
        default_factory=list,
        description="HA sensor entities used for decisions/why.",
    )
    moisture_start_below: float | None = Field(
        default=None,
        description="Start watering if moisture is below this value (units depend on sensor).",
    )
    plant_ids: list[str] = Field(
        default_factory=list,
        description="Plant IDs linked to this controller, e.g. ['monstera','ficus'].",
    )

    agent_id: str = Field(default="home1", description="Which agent executes HA calls (MVP default)")
    requires_agent: bool = Field(
        default=True,
        description="Whether runtime actions must be executed through a connected agent.",
    )
    zone: Zone | None = Field(default=None, description="Zone (category) to which the controller belongs to.")


class WateringControllerUpdate(BaseModel):
    display_name: str

    actuator_entity_ids: list[str] = Field(
        default_factory=list,
        description="HA entities to switch ON/OFF, e.g. ['switch.garden_pump','valve.zone_1']",
    )
    moisture_sensor_entity_ids: list[str] = Field(
        default_factory=list,
        description="HA sensor entities used for decisions/why.",
    )
    moisture_start_below: float | None = Field(
        default=None,
        description="Start watering if moisture is below this value (units depend on sensor).",
    )
    plant_ids: list[str] = Field(
        default_factory=list,
        description="Plant IDs linked to this controller, e.g. ['monstera','ficus'].",
    )
    agent_id: str = Field(default="home1", description="Which agent executes HA calls (MVP default)")
    requires_agent: bool = Field(
        default=True,
        description="Whether runtime actions must be executed through a connected agent.",
    )
    zone: Zone | None = Field(default=None, description="Zone (category) to which the controller belongs to.")

class WateringControllerView(BaseModel):
    controller_id: str
    display_name: str
    actuator_entity_ids: list[str]
    agent_id: str
    requires_agent: bool = True
    moisture_sensor_entity_ids: list[str] = Field(default_factory=list)
    moisture_start_below: float | None = None
    zone: Zone | None = None
    plant_ids: list[str] = Field(default_factory=list)
    icon_keys: list[str] = Field(default_factory=list)
    plant_count: int = 0

class WateringStartRequest(BaseModel):
    seconds: int = Field(ge=1, le=3600, description="How long to water")

class WateringWhyView(BaseModel):
    controller_id: str
    decision: Literal["start", "skip", "unknown"]
    reason_code: str
    message: str
    at: datetime

    moisture_sensor_entity_ids: list[str] = Field(default_factory=list)
    moisture_state_raw: str | None = None
    moisture_value: float | None = None
    moisture_start_below: float | None = None


class WateringNextView(BaseModel):
    controller_id: str
    next_run_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    last_outcome: str | None = None
    last_reason_code: str | None = None
    last_error: str | None = None
    is_paused: bool = False
    skip_next_pending: bool = False
