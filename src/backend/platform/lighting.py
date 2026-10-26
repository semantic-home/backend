"""Natural dawn light levels and app-owned, local Home Assistant execution."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import Observer, SunDirection
from astral.sun import elevation, time_at_elevation

from backend.semantic.capabilities import infer_capabilities
from .models import AutomationSpec, DawnRamp, HomeLocation
from .solar import event_time

PHASES = (("astronomical", "Astronomical", -18), ("nautical", "Nautical", -12),
          ("civil", "Civil", -6), ("daylight", "Daylight", 0))


def phase_crossing(observer: Observer, angle: float, day, zone: ZoneInfo) -> datetime | None:
    # HA sun.sun reports Astral's apparent elevation, which does not include
    # observer-height horizon dip. time_at_elevation includes that dip and uses
    # a refraction approximation, so refine its sea-level estimate against the
    # very same elevation function used by HA and the curve below.
    try:
        guess = time_at_elevation(Observer(observer.latitude, observer.longitude), angle, day, SunDirection.RISING, zone).astimezone(timezone.utc)
    except ValueError:
        return None
    for minutes in (15, 30, 60):
        low, high = guess - timedelta(minutes=minutes), guess + timedelta(minutes=minutes)
        if elevation(observer, low) <= angle <= elevation(observer, high):
            for _ in range(25):
                middle = low + (high-low)/2
                if elevation(observer, middle) < angle:
                    low = middle
                else:
                    high = middle
            return high.astimezone(zone)
    return None


def ramp_level(ramp: DawnRamp, angle: float, elapsed_minutes: float) -> dict:
    """Interpolate by the real sun angle, with a dim-start catch-up fade."""
    levels = [getattr(ramp, key) for key, _, _ in PHASES]
    position = max(0, min(3, (angle + 18) / 6))
    left = min(2, int(position))
    fraction = position - left
    startup = max(0, min(1, elapsed_minutes / ramp.startup_minutes))
    result = {}
    for field, initial in (("brightness_pct", ramp.initial_brightness_pct), ("kelvin", levels[0].kelvin)):
        natural = getattr(levels[left], field) + (getattr(levels[left + 1], field) - getattr(levels[left], field)) * fraction
        result[field] = round(initial + (natural - initial) * startup)
    return result


def ramp_preview(spec: AutomationSpec, metadata: dict, connection_name: str, household_timezone: str,
                 now: datetime, connected: bool, targets: list[dict]) -> dict:
    zone, ramp = ZoneInfo(household_timezone), spec.ramp
    local = now.astimezone(zone)
    result = {"timezone": household_timezone, "date": local.date().isoformat(), "location_name": None,
              "connection_name": connection_name, "connected": connected, "available": False,
              "now": local.isoformat(), "start": None, "stop": None, "phases": [], "samples": [],
              "warnings": [], "lamps": [], "selected_day": local.weekday() in spec.weekdays}
    if not metadata.get("location"):
        result["warnings"].append("Home Assistant has not reported its home location. Update or reconnect the local agent to enable the preview.")
        return result
    location = HomeLocation.model_validate(metadata["location"])
    result["location_name"] = location.name
    if metadata.get("timezone") != household_timezone:
        result["warnings"].append("Home Assistant and household time zones differ. Match them before activating this rule.")
        return result
    if metadata.get("stub"):
        result["warnings"].append("This connection is a simulation; these are example times.")
    if not connected:
        result["warnings"].append("The agent is offline. Times use its last reported home location.")
    observer = Observer(location.latitude, location.longitude, location.elevation)
    start = event_time("time", ramp.start_at, 0, local.date(), zone, location)
    stop = event_time("time", ramp.stop_at, 0, local.date(), zone, location)
    result["start"], result["stop"] = start.isoformat() if start else None, stop.isoformat() if stop else None
    if not start or not stop:
        result["warnings"].append("A chosen clock time does not exist today because of daylight saving. That clock trigger is skipped.")
    elif start.utcoffset() != start.replace(fold=1).utcoffset():
        result["warnings"].append("The start time occurs twice today because of daylight saving. Home Assistant starts at both occurrences; the chart shows the first.")
    for key, label, angle in PHASES:
        at = phase_crossing(observer, angle, local.date(), zone)
        result["phases"].append({"key": key, "label": label, "elevation": angle, "at": at.isoformat() if at else None,
                                 "before_start": bool(at and start and at < start), **getattr(ramp, key).model_dump()})
    if any(p["at"] is None for p in result["phases"]):
        result["warnings"].append("Some dawn phases do not occur today at this latitude. The ramp follows the available rising sun angle; it does not invent missing phases or run backwards after solar noon.")
    if start and stop:
        start_utc, stop_utc = start.astimezone(timezone.utc), stop.astimezone(timezone.utc)
        times = {start_utc, stop_utc, min(stop_utc, start_utc + timedelta(minutes=ramp.startup_minutes))}
        times.update(start_utc + timedelta(minutes=n) for n in range(0, int((stop_utc-start_utc).total_seconds()/60), 5))
        times.update(datetime.fromisoformat(p["at"]).astimezone(timezone.utc) for p in result["phases"] if p["at"] and start < datetime.fromisoformat(p["at"]) < stop)
        previous = {"brightness_pct": ramp.initial_brightness_pct, "kelvin": ramp.astronomical.kelvin}
        for at in sorted(times):
            angle = elevation(observer, at)
            rising = elevation(observer, at + timedelta(seconds=30)) >= angle
            # A descending, below-horizon sun must never reverse a morning ramp.
            if rising or angle >= 0:
                previous = ramp_level(ramp, angle, (at-start_utc).total_seconds()/60)
            result["samples"].append({"at": at.astimezone(zone).isoformat(), **previous})
        if ramp.off_at_end:
            result["samples"].append({"at": stop.isoformat(), **previous, "brightness_pct": 0})
    for target in targets:
        caps = infer_capabilities(target["entity_id"], target["attributes"])
        color = caps.get("color_temp") if ramp.color_mode == "white" else None
        result["lamps"].append({"name": target["attributes"].get("friendly_name", target["entity_id"]),
                                "color_temp": color, "transition": caps.get("transition", False)})
    result["available"] = bool(start and stop)
    return result


def _natural_expression(ramp: DawnRamp, field: str) -> str:
    values = [getattr(getattr(ramp, key), field) for key, _, _ in PHASES]
    expression = str(values[-1])
    for index in reversed(range(3)):
        angle = PHASES[index][2]
        segment = f"({values[index]} + ({values[index+1]} - {values[index]}) * (ramp_elevation - ({angle})) / 6)"
        expression = f"({segment} if ramp_elevation < {angle+6} else {expression})"
    return f"({values[0]} if ramp_elevation <= -18 else {expression})"


def compile_ramp(config: dict, rule_id: str, spec: AutomationSpec, targets: list[dict]) -> dict:
    # Only validated numeric settings and JSON-quoted clocks/IDs enter templates.
    ramp = spec.ramp
    start, stop = json.dumps(ramp.start_at), json.dumps(ramp.stop_at)
    def template(expression):
        return "{{ " + expression + " }}"
    config["triggers"] = [
        {"trigger": "time", "at": ramp.start_at + ":00", "id": "ramp_start"},
        {"trigger": "time", "at": ramp.stop_at + ":00", "id": "ramp_stop"},
        {"trigger": "time_pattern", "minutes": "/1", "id": "ramp_tick"},
        {"trigger": "homeassistant", "event": "start", "id": "ramp_resume"},
        {"trigger": "event", "event_type": "automation_reloaded", "id": "ramp_resume"},
    ]
    active = f"now().weekday() in {json.dumps(spec.weekdays)} and {start} <= now().strftime('%H:%M') < {stop}"
    valid_day = f"as_local(as_datetime(as_timestamp(today_at({start})))).strftime('%H:%M') == {start}"
    active = f"({active} and {valid_day})"
    initial = "trigger.id | default('') == 'ramp_start'"
    valid_sun = "is_number(state_attr('sun.sun', 'elevation')) and (state_attr('sun.sun', 'rising') == true or ramp_elevation >= 0)"
    config["actions"] = [{"variables": {
        "ramp_elevation": template("state_attr('sun.sun', 'elevation') | float(-90)"),
        "ramp_elapsed": template(f"0 if {initial} else [0, (as_timestamp(now()) - as_timestamp(today_at({start}).replace(fold=now().fold))) / 60] | max"),
    }}, {"variables": {"ramp_startup": template(f"[1, ramp_elapsed / {ramp.startup_minutes}] | min")}},
        {"variables": {"ramp_brightness": template(f"({ramp.initial_brightness_pct} + ({_natural_expression(ramp, 'brightness_pct')} - {ramp.initial_brightness_pct}) * ramp_startup) | round(0) | int"),
                       "ramp_kelvin": template(f"({ramp.astronomical.kelvin} + ({_natural_expression(ramp, 'kelvin')} - {ramp.astronomical.kelvin}) * ramp_startup) | round(0) | int")}}]
    for device in targets:
        eid, attrs = device["entity_id"], device["attributes"]
        # Validate the ID even for direct compiler callers.
        from .compiler import _entity
        quoted = _entity(eid)
        caps = infer_capabilities(eid, attrs)
        if not eid.startswith("light.") or not caps.get("brightness"):
            raise ValueError("A dawn ramp requires dimmable lights")
        data = {"brightness_pct": template("ramp_brightness")}
        changed = f"(((state_attr({quoted}, 'brightness') | float(0)) / 255 * 100 - ramp_brightness) | abs >= 1)"
        supported = f"(state_attr({quoted}, 'supported_color_modes') or []) | reject('in', ['onoff', 'unknown']) | list | length > 0"
        if ramp.color_mode == "white":
            color = caps.get("color_temp")
            if not color:
                raise ValueError("White dawn ramps require colour temperature support")
            # Use current limits as well as identity guards if an integration
            # updates a lamp's capabilities after deployment.
            kelvin = f"([[ramp_kelvin, state_attr({quoted}, 'min_color_temp_kelvin') | int({color['min_kelvin']})] | max, state_attr({quoted}, 'max_color_temp_kelvin') | int({color['max_kelvin']})] | min)"
            data["color_temp_kelvin"] = template(kelvin)
            changed += f" or (((state_attr({quoted}, 'color_temp_kelvin') | float(0)) - {kelvin}) | abs >= 25)"
            supported = f"'color_temp' in (state_attr({quoted}, 'supported_color_modes') or [])"
        if caps.get("transition"):
            data["transition"] = template(f"0 if {initial} else 60")
        guard = [{"condition": "template", "value_template": template(supported)}]
        if attrs.get("device_id"):
            guard.append({"condition": "template", "value_template": template(f"device_id({quoted}) == {json.dumps(attrs['device_id'])}")})
        event = {"event": "semantic_home_action", "event_data": {"automation_id": rule_id, "entity_id": eid,
                                                                    "desired": "on", "reason": "lighting_ramp"}}
        branches = [{"conditions": [*guard, {"condition": "template", "value_template": template(
            f"{active} and (({initial} and states({quoted}) in ['on', 'off']) or "
            f"(is_state({quoted}, 'on') and ({valid_sun}) and ({changed})))")}],
            "sequence": [{"action": "light.turn_on", "target": {"entity_id": eid}, "data": data, "continue_on_error": True}, event]}]
        if ramp.off_at_end:
            # Off is independent of sun availability and only applies on the
            # selected day. Missed stop events are not replayed on restart.
            branches.insert(0, {"conditions": [*guard, {"condition": "template", "value_template": template(
                f"trigger.id | default('') == 'ramp_stop' and now().weekday() in {json.dumps(spec.weekdays)} and is_state({quoted}, 'on')")}],
                "sequence": [{"action": "light.turn_off", "target": {"entity_id": eid}, "continue_on_error": True},
                             {"event": "semantic_home_action", "event_data": {"automation_id": rule_id, "entity_id": eid, "desired": "off", "reason": "ramp_finished"}}]})
        config["actions"].append({"choose": branches})
    return config
