"""Location-aware previews for HA-native solar triggers. No device commands."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sunrise, sunset

from .models import AutomationSpec, HomeLocation


def event_time(event: str, clock: str | None, offset: int, day: date,
               zone: ZoneInfo, location: HomeLocation) -> datetime | None:
    if event == "time":
        # HA skips a nonexistent spring-forward time. Repeated autumn times
        # are expanded to both folds by solar_preview below.
        candidate = datetime.combine(day, time.fromisoformat(clock), zone)
        if candidate.astimezone(timezone.utc).astimezone(zone) != candidate:
            return None
        return candidate
    try:
        observer = Observer(location.latitude, location.longitude, location.elevation)
        raw = {"sunrise": sunrise, "sunset": sunset}[event](observer, day, tzinfo=zone)
        # An offset is elapsed time, including when it crosses a DST boundary.
        return (raw.astimezone(timezone.utc) + timedelta(minutes=offset)).astimezone(zone)
    except ValueError:
        return None  # Polar day/night: no event on this date.


def solar_preview(spec: AutomationSpec, metadata: dict, connection_name: str,
                  household_timezone: str, now: datetime, connected: bool) -> dict:
    zone = ZoneInfo(household_timezone)
    local = now.astimezone(zone)
    result = {"timezone": household_timezone, "location_name": None,
              "connection_name": connection_name, "connected": connected,
              "now": local.isoformat(), "sunrise": None, "sunset": None,
              "events": [], "warnings": [], "available": False}
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
    for event in ("sunrise", "sunset"):
        value = event_time(event, None, 0, local.date(), zone, location)
        result[event] = value.isoformat() if value else None
    missing = False
    # Include yesterday for offsets crossing midnight. Eight days cover the next
    # occurrence even when only one weekday is selected.
    for delta in range(-1, 9):
        day = local.date() + timedelta(days=delta)
        for edge, action in (("start", "on"), ("end", "off")):
            if edge == "start" and day.weekday() not in spec.weekdays:
                continue
            event, offset = getattr(spec, edge + "_event"), getattr(spec, edge + "_offset_minutes")
            value = event_time(event, getattr(spec, edge), offset, day, zone, location)
            if value is None:
                missing = True
                continue
            values = [value]
            if event == "time" and value.utcoffset() != value.replace(fold=1).utcoffset():
                values.append(value.replace(fold=1))
            for candidate in values:
                if candidate.astimezone(timezone.utc) > now:
                    result["events"].append({"at": candidate.isoformat(), "action": action,
                                             "event": event, "offset_minutes": offset,
                                             "anchor_date": day.isoformat()})
    result["events"] = sorted(result["events"], key=lambda e: datetime.fromisoformat(e["at"]).timestamp())[:12]
    if missing:
        result["warnings"].append("Some dates have no solar event or a clock time skipped by daylight saving. That trigger does not run on those dates.")
    result["available"] = True
    return result
