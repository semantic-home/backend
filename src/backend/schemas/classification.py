from __future__ import annotations

from pydantic import BaseModel


class EntityClassificationView(BaseModel):
    entity_id: str
    semantic_type: str      # e.g. "moisture_sensor", "water_pump"
    role: str               # "actuator" | "sensor" | "binary_sensor" | "panel" | "camera" | "unknown"
    domain_hint: str | None # "watering" | "lighting" | "power" | "surveillance" | None
    confidence: str         # "high" | "medium" | "low"
    label: str              # human-readable label
    zone_hint: str | None = None  # inferred area, e.g. "living_room", "bedroom", "kitchen"
    suggested_moisture_sensor_entity_ids: list[str] = []


class ClassificationOverride(BaseModel):
    role: str | None = None
    domain_hint: str | None = None
