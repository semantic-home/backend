from __future__ import annotations

from typing import Any, Dict

def infer_capabilities(entity_id: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
    domain: str = entity_id.split(".", 1)[0]

    if domain in ("switch", "humidifier"):
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
        caps = {"actions": ["turn_on", "turn_off"]}
        modes = set(attributes.get("supported_color_modes") or [])
        if "brightness" in attributes or modes - {"onoff", "unknown"}:
            caps["brightness"] = {"supported": True}
            caps["actions"].append("set_brightness")
        low, high = attributes.get("min_color_temp_kelvin"), attributes.get("max_color_temp_kelvin")
        if "color_temp" in modes and isinstance(low, (int, float)) and isinstance(high, (int, float)) and 1000 <= low <= high <= 10000:
            caps["color_temp"] = {"supported": True, "min_kelvin": int(low), "max_kelvin": int(high)}
        features = attributes.get("supported_features", 0)
        caps["transition"] = isinstance(features, int) and bool(features & 32)
        return caps

    if domain == "valve":
        return {"actions": ["open_valve", "close_valve"]}

    if domain == "vacuum":
        features = attributes.get("supported_features", 0)
        features = features if isinstance(features, int) else 0
        return {"actions": [name for bit, name in ((8192, "start"), (4, "pause"), (16, "return_to_base")) if features & bit]}

    return {
        "read_only": True
    }
