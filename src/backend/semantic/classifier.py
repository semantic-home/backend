from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, Protocol


@dataclass(frozen=True)
class EntityClassification:
    entity_id: str
    semantic_type: str      # e.g. "moisture_sensor", "water_pump", "motion_sensor"
    role: str               # "actuator" | "sensor" | "binary_sensor" | "panel" | "camera" | "unknown"
    domain_hint: str | None # "watering" | "lighting" | "power" | "surveillance" | None
    confidence: str         # "high" | "medium" | "low"
    label: str              # human-readable: "Soil Moisture Sensor", "Water Pump"
    zone_hint: str | None = None  # inferred area/room, e.g. "living_room", "bedroom", "kitchen"


class SupportsClassification(Protocol):
    entity_id: str
    semantic_type: str
    role: str
    domain_hint: str | None
    label: str
    zone_hint: str | None


_GENERIC_WATERING_MATCH_TOKENS = frozenset({
    "switch",
    "sensor",
    "binary",
    "pump",
    "valve",
    "soil",
    "moisture",
    "water",
    "watering",
    "irrigation",
    "living",
    "room",
    "bedroom",
    "kitchen",
    "bathroom",
    "home",
    "office",
    "tank",
    "level",
})

_NEGATIVE_MOISTURE_SENSOR_TOKENS = frozenset({
    "battery",
    "batterie",
    "akku",
    "voltage",
    "volt",
    "rssi",
    "dbm",
})


def _slug_tokens(entity_id: str) -> frozenset[str]:
    """Lowercase tokens from the entity slug (part after the first dot)."""
    slug = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
    return frozenset(slug.replace("-", "_").split("_"))


def _classification_tokens(entity_id: str, attributes: dict[str, Any]) -> frozenset[str]:
    friendly_name = attributes.get("friendly_name")
    label_tokens = _text_tokens(friendly_name) if isinstance(friendly_name, str) else frozenset()
    return _slug_tokens(entity_id) | label_tokens


def _infer_zone_hint(attributes: dict[str, Any], tokens: frozenset[str]) -> str | None:
    """Try to infer the room/area from HA area_id attribute or entity name tokens."""
    area_id = attributes.get("area_id")
    if area_id:
        return str(area_id)
    if "living" in tokens:
        return "living_room"
    if "bedroom" in tokens:
        return "bedroom"
    if "kitchen" in tokens:
        return "kitchen"
    if "bathroom" in tokens:
        return "bathroom"
    if "office" in tokens:
        return "home_office"
    return None


def _display_label(attributes: dict[str, Any], fallback: str) -> str:
    friendly_name = attributes.get("friendly_name")
    if isinstance(friendly_name, str) and friendly_name.strip():
        return friendly_name.strip()
    return fallback


def _text_tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", value.lower()))


def _meaningful_match_tokens(entity_id: str, label: str) -> frozenset[str]:
    return frozenset(
        token
        for token in (_slug_tokens(entity_id) | _text_tokens(label))
        if token not in _GENERIC_WATERING_MATCH_TOKENS
        and not token.isdigit()
        and len(token) > 1
    )


def _watering_base_slug(entity_id: str, *, remove: frozenset[str]) -> tuple[str, ...]:
    slug = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
    return tuple(
        token
        for token in slug.replace("-", "_").split("_")
        if token
        and token not in remove
        and not token.isdigit()
    )


def _moisture_sensor_match_score(
    actuator: SupportsClassification,
    sensor: SupportsClassification,
    *,
    total_sensor_count: int,
) -> int:
    score = 0
    actuator_base = _watering_base_slug(actuator.entity_id, remove=frozenset({"switch", "pump", "valve"}))
    sensor_base = _watering_base_slug(sensor.entity_id, remove=frozenset({"sensor", "soil", "moisture", "probe"}))
    if actuator_base and sensor_base and actuator_base == sensor_base:
        score += 100

    overlap = _meaningful_match_tokens(actuator.entity_id, actuator.label).intersection(
        _meaningful_match_tokens(sensor.entity_id, sensor.label)
    )
    score += 20 * len(overlap)

    if actuator.zone_hint and sensor.zone_hint and actuator.zone_hint == sensor.zone_hint:
        score += 15
    elif actuator.zone_hint and sensor.zone_hint and actuator.zone_hint != sensor.zone_hint:
        score -= 10

    if score <= 0 and total_sensor_count == 1:
        score = 1

    return score


def suggest_moisture_sensor_candidates(
    classifications: list[SupportsClassification],
) -> dict[str, list[str]]:
    actuators = [
        classification
        for classification in classifications
        if classification.domain_hint == "watering"
        and classification.role == "actuator"
        and classification.semantic_type == "water_pump"
    ]
    sensors = [
        classification
        for classification in classifications
        if classification.domain_hint == "watering"
        and classification.role == "sensor"
        and classification.semantic_type == "moisture_sensor"
    ]

    suggestions: dict[str, list[str]] = {}
    for actuator in actuators:
        ranked = sorted(
            (
                (_moisture_sensor_match_score(actuator, sensor, total_sensor_count=len(sensors)), sensor.label.casefold(), sensor.entity_id)
                for sensor in sensors
            ),
            key=lambda item: (-item[0], item[1], item[2]),
        )
        matching = [
            entity_id
            for score, _, entity_id in ranked
            if score > 0
        ]
        if matching:
            suggestions[actuator.entity_id] = matching
            continue

        suggestions[actuator.entity_id] = [
            entity_id
            for _, _, entity_id in ranked
        ]
    return suggestions


def classify_entity(
    entity_id: str,
    state: str,  # noqa: ARG001 — reserved for future state-aware classification
    attributes: dict[str, Any],
) -> EntityClassification:
    tokens = _classification_tokens(entity_id, attributes)
    base = _classify_type(entity_id, attributes, tokens)
    zone_hint = _infer_zone_hint(attributes, tokens)
    if zone_hint is not None:
        return replace(base, zone_hint=zone_hint)
    return base


def _classify_type(
    entity_id: str,
    attributes: dict[str, Any],
    tokens: frozenset[str],
) -> EntityClassification:
    ha_domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
    device_class: str | None = attributes.get("device_class")
    unit: str | None = attributes.get("unit_of_measurement")

    # --- Camera ---
    if ha_domain == "camera":
        return EntityClassification(
            entity_id=entity_id,
            semantic_type="camera",
            role="camera",
            domain_hint="surveillance",
            confidence="high",
            label=_display_label(attributes, "Camera"),
        )

    # --- Alarm panel ---
    if ha_domain == "alarm_control_panel":
        return EntityClassification(
            entity_id=entity_id,
            semantic_type="alarm_panel",
            role="panel",
            domain_hint="surveillance",
            confidence="high",
            label=_display_label(attributes, "Alarm Panel"),
        )

    # --- Light ---
    if ha_domain == "light":
        return EntityClassification(
            entity_id=entity_id,
            semantic_type="light_actuator",
            role="actuator",
            domain_hint="lighting",
            confidence="high",
            label=_display_label(attributes, "Light"),
        )

    # --- Binary sensors ---
    if ha_domain == "binary_sensor":
        if device_class == "motion" or "motion" in tokens:
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="motion_sensor",
                role="binary_sensor",
                domain_hint="surveillance",
                confidence="high" if device_class == "motion" else "medium",
                label=_display_label(attributes, "Motion Sensor"),
            )
        if device_class in ("window", "door", "opening") or tokens & {"window", "door", "contact", "opening"}:
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="contact_sensor",
                role="binary_sensor",
                domain_hint="surveillance",
                confidence="high" if device_class in ("window", "door", "opening") else "medium",
                label=_display_label(attributes, "Contact Sensor"),
            )
        if device_class == "moisture" or "leak" in tokens:
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="leak_sensor",
                role="binary_sensor",
                domain_hint="surveillance",
                confidence="high" if device_class == "moisture" else "medium",
                label=_display_label(attributes, "Leak Sensor"),
            )
        return EntityClassification(
            entity_id=entity_id,
            semantic_type="generic_binary_sensor",
            role="binary_sensor",
            domain_hint=None,
            confidence="low",
            label=_display_label(attributes, "Binary Sensor"),
        )

    # --- Switch / Valve domain ---
    if ha_domain in ("switch", "valve"):
        if device_class == "outlet":
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="power_outlet",
                role="actuator",
                domain_hint="power",
                confidence="high",
                label=_display_label(attributes, "Power Outlet"),
            )
        if tokens & {"pump", "watering", "irrigation", "valve"}:
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="water_pump",
                role="actuator",
                domain_hint="watering",
                confidence="medium",
                label=_display_label(attributes, "Water Pump"),
            )
        return EntityClassification(
            entity_id=entity_id,
            semantic_type="generic_switch",
            role="actuator",
            domain_hint=None,
            confidence="low",
            label=_display_label(attributes, "Switch"),
        )

    # --- Sensors ---
    if ha_domain == "sensor":
        has_negative_moisture_tokens = bool(tokens & _NEGATIVE_MOISTURE_SENSOR_TOKENS) or device_class in {
            "battery",
            "voltage",
            "signal_strength",
        }
        has_moisture_signal = device_class == "moisture" or (
            not has_negative_moisture_tokens and (
                "moisture" in tokens
                or (unit == "%" and "soil" in tokens)
            )
        )
        if has_moisture_signal:
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="moisture_sensor",
                role="sensor",
                domain_hint="watering",
                confidence=(
                    "high" if device_class == "moisture"
                    else "medium" if unit == "%"
                    else "low"
                ),
                label=_display_label(attributes, "Soil Moisture Sensor"),
            )
        if unit == "%" and tokens & {"tank", "reservoir", "level", "water"}:
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="tank_level_sensor",
                role="sensor",
                domain_hint="watering",
                confidence="medium",
                label=_display_label(attributes, "Tank Level Sensor"),
            )
        if device_class == "illuminance" or unit == "lx":
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="illuminance_sensor",
                role="sensor",
                domain_hint="lighting",
                confidence="high" if device_class == "illuminance" else "medium",
                label=_display_label(attributes, "Illuminance Sensor"),
            )
        if device_class == "power" or (unit is not None and unit in ("W", "kW")):
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="power_meter",
                role="sensor",
                domain_hint="power",
                confidence="high" if device_class == "power" else "medium",
                label=_display_label(attributes, "Power Meter"),
            )
        if unit is not None and ("eur" in unit.lower() or "kwh" in unit.lower()):
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="energy_price_sensor",
                role="sensor",
                domain_hint="power",
                confidence="medium",
                label=_display_label(attributes, "Energy Price Sensor"),
            )
        if tokens & {"nvr", "storage", "surveillance"}:
            return EntityClassification(
                entity_id=entity_id,
                semantic_type="nvr_storage_sensor",
                role="sensor",
                domain_hint="surveillance",
                confidence="medium",
                label=_display_label(attributes, "NVR Storage Sensor"),
            )
        return EntityClassification(
            entity_id=entity_id,
            semantic_type="generic_sensor",
            role="sensor",
            domain_hint=None,
            confidence="low",
            label=_display_label(attributes, "Sensor"),
        )

    return EntityClassification(
        entity_id=entity_id,
        semantic_type="unknown",
        role="unknown",
        domain_hint=None,
        confidence="low",
        label=_display_label(attributes, "Unknown"),
    )
