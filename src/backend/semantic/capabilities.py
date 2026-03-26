from __future__ import annotations

from typing import Any, Dict

def infer_capabilities(entity_id: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
    domain: str = entity_id.split(".", 1)[0]

    if domain in ("switch", "valve"):
        return {
            "actions": ["turn_on", "turn_off"]
        }

    if domain == "sensor":
        return {
            "read_only": True,
            "unit": attributes.get("unit_of_measurement"),
            "device_class": attributes.get("device_class"),
        }

    if domain == "light":
        # MVP: nur rudimentär, später feiner (brightness/color_temp/transition)
        caps = {"actions": ["turn_on", "turn_off"]}
        if "brightness" in attributes:
            caps["brightness"] = {"supported": True}
        return caps

    return {
        "read_only": True
    }