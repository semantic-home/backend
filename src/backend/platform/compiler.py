"""Generate app-owned HA configurations from validated data, never user Jinja."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import AutomationSpec


def window_active(spec: AutomationSpec, now: datetime, timezone: str) -> bool:
    if spec.uses_sun:
        raise ValueError("Solar schedules are event triggers, not continuously enforced time windows")
    local = now.astimezone(ZoneInfo(timezone))
    clock, day = local.strftime("%H:%M"), local.weekday()
    if not spec.start:
        return day in spec.weekdays
    if spec.start < spec.end:
        return day in spec.weekdays and spec.start <= clock < spec.end
    if clock < spec.end:
        return (local - timedelta(days=1)).weekday() in spec.weekdays
    return day in spec.weekdays and clock >= spec.start


def humidity_decision(spec: AutomationSpec, value: float | None, is_on: bool, active: bool):
    if value is None:
        return False, "sensor_unavailable"
    if not active:
        return False, "outside_window"
    if value >= spec.upper:
        return True, "upper_threshold"
    if value <= spec.lower:
        return False, "lower_threshold"
    return is_on, "keep_state"


def _entity(value):
    if not re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", value):
        raise ValueError("Invalid Home Assistant entity ID")
    return json.dumps(value)


def _window(spec):
    days = json.dumps(spec.weekdays)
    if not spec.start:
        return f"now().weekday() in {days}"
    start, end, clock = json.dumps(spec.start), json.dumps(spec.end), "now().strftime('%H:%M')"
    if spec.start < spec.end:
        return f"(now().weekday() in {days} and {start} <= {clock} < {end})"
    return (f"((now().weekday() in {days} and {clock} >= {start}) or "
            f"((now().weekday() - 1) % 7 in {days} and {clock} < {end}))")


def _schedule_triggers(spec):
    triggers = []
    for edge, action in (("start", "on"), ("end", "off")):
        event = getattr(spec, edge + "_event")
        trigger = {"trigger": "time", "at": getattr(spec, edge) + ":00"} if event == "time" else {
            "trigger": "sun", "event": event, "offset": getattr(spec, edge + "_offset_minutes") * 60}
        triggers.append({**trigger, "id": "schedule_" + action})
    return triggers


def compile_automation(rule_id: str, spec: AutomationSpec, targets: list[dict], sensors: list[dict] | dict | None, version: int):
    # Legacy callers may still supply one sensor record.
    sensors = [sensors] if isinstance(sensors, dict) else sensors or []
    owned_id = "semantic_home_" + rule_id.replace("-", "")
    if not re.fullmatch(r"semantic_home_[a-f0-9]{32}", owned_id):
        raise ValueError("Invalid automation ID")
    config = {
        "id": owned_id, "alias": f"Semantic Home · {spec.name}",
        "description": f"Managed by Semantic Home; id={rule_id}; version={version}",
        "initial_state": spec.enabled, "mode": "single", "max_exceeded": "silent",
        "triggers": [], "conditions": [], "actions": [],
        "variables": {"semantic_home_bindings": {d["entity_id"]: d["attributes"].get("registry_id")
                                                 for d in targets + sensors}},
    }
    if spec.kind == "cleaning":
        config["triggers"] = [{"trigger": "time", "at": spec.at + ":00"}]
        config["conditions"] = [{"condition": "template", "value_template": "{{ " + _window(spec) + " }}"}]
        config["conditions"].append({"condition": "template", "value_template": "{{ this.attributes.last_triggered is none or as_local(this.attributes.last_triggered).date() != now().date() }}"})
        config["actions"] = [
            {"action": "vacuum.start", "target": {"entity_id": d["entity_id"]}, "continue_on_error": True}
            for d in targets
        ]
        config["actions"] = [step for action, device in zip(config["actions"], targets) for step in [action,
            {"event": "semantic_home_action", "event_data": {"automation_id": rule_id, "entity_id": device["entity_id"], "desired": "cleaning", "reason": "scheduled_cleaning"}}]]
        for d in targets:
            if d["attributes"].get("device_id"):
                config["conditions"].append({"condition": "template", "value_template": "{{ device_id(" + _entity(d["entity_id"]) + ") == " + json.dumps(d["attributes"]["device_id"]) + " }}"})
        return config
    config["triggers"] = _schedule_triggers(spec) if spec.uses_sun else [
        {"trigger": "homeassistant", "event": "start"},
        {"trigger": "event", "event_type": "automation_reloaded"},
        {"trigger": "time_pattern", "minutes": "/1"},
    ]
    if spec.uses_sun:
        # A slow target must not discard a following off event.
        config["mode"] = "queued"
    if sensors:
        config["triggers"].append({"trigger": "state", "entity_id": [sensor["entity_id"] for sensor in sensors]})
    active = _window(spec) if not spec.uses_sun else "false"
    if sensors:
        valid_readings, values = [], []
        for sensor in sensors:
            sid = _entity(sensor["entity_id"])
            valid = (f"(is_number(states({sid})) and 0 <= states({sid}) | float(0) <= 100 and "
                     f"0 <= as_timestamp(now()) - as_timestamp(states[{sid}].last_updated) < {spec.sensor_stale_seconds})")
            if sensor["attributes"].get("device_id"):
                valid = f"({valid} and device_id({sid}) == {json.dumps(sensor['attributes']['device_id'])})"
            valid_readings.append(valid)
            values.append(f"states({sid}) | float(0)")
        valid = "(" + " and ".join(valid_readings) + ")"
        measurement = "([" + ", ".join(values) + "] | max)"
    for device in targets:
        eid = device["entity_id"]
        quoted, domain = _entity(eid), eid.split(".")[0]
        if sensors:
            off = f"(not ({active}) or not {valid} or {measurement} <= {spec.lower})"
            on = f"(({active}) and {valid} and {measurement} >= {spec.upper})"
            elapsed = f"(as_timestamp(now()) - as_timestamp(states[{quoted}].last_changed) >= {spec.min_cycle_seconds})"
            off = f"({off} and ({elapsed} or not {valid} or not ({active})))"
            on = f"({on} and {elapsed})"
        else:
            off, on = f"not ({active})", active
        if spec.uses_sun:
            # Weekdays refer to the unshifted sun event's local date. A late
            # sunset + offset can run after midnight without changing its day.
            day = ("as_local(as_datetime(as_timestamp(now()) - "
                   + str(spec.start_offset_minutes * 60) + ")).weekday()")
            on = f"trigger.id == 'schedule_on' and {day} in {json.dumps(spec.weekdays)}"
            off = "trigger.id == 'schedule_off'"
        on_state = {"condition": "state", "entity_id": eid, "state": "off"}
        if spec.brightness_pct is not None:
            on_state = {"condition": "template", "value_template": "{{ is_state(" + quoted + ", 'off') or (is_state(" + quoted + ", 'on') and ((state_attr(" + quoted + ", 'brightness') | float(0)) / 255 * 100 - " + str(spec.brightness_pct) + ") | abs >= 1) }}"}
        on_action = {"action": f"{domain}.turn_on", "target": {"entity_id": eid}, "continue_on_error": True}
        if spec.brightness_pct is not None:
            on_action["data"] = {"brightness_pct": spec.brightness_pct}
        config["actions"].append({"choose": [
            {"conditions": [
                {"condition": "state", "entity_id": eid, "state": "on"},
                {"condition": "template", "value_template": "{{ " + off + " }}"}],
             "sequence": [{"action": f"{domain}.turn_off", "target": {"entity_id": eid}, "continue_on_error": True}]},
            {"conditions": [
                on_state,
                {"condition": "template", "value_template": "{{ " + on + " }}"}],
             "sequence": [on_action]},
        ]})
        for index, branch in enumerate(config["actions"][-1]["choose"]):
            reason = ("{{ 'sensor_unavailable' if not (" + valid + ") else 'outside_window' if not (" + active + ") else 'lower_threshold' }}"
                      if sensors and index == 0 else "upper_threshold" if sensors else "solar_schedule" if spec.uses_sun else "time_window")
            branch["sequence"].append({"event": "semantic_home_action", "event_data": {
                "automation_id": rule_id, "entity_id": eid, "desired": "off" if index == 0 else "on", "reason": reason}})
        if device["attributes"].get("device_id"):
            guard = {"condition": "template", "value_template": "{{ device_id(" + quoted + ") == " + json.dumps(device["attributes"]["device_id"]) + " }}"}
            for branch in config["actions"][-1]["choose"]:
                branch["conditions"].append(guard)
    return config
