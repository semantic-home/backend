from __future__ import annotations

from datetime import time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class HomeCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    timezone: str = "Europe/Berlin"

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown timezone")
        return value


class Credentials(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=256)
    registration_key: str = ""

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.strip().lower()
        if "@" not in value or any(c.isspace() for c in value):
            raise ValueError("Invalid email")
        return value


class SensorSelection(StrictModel):
    # Keep the legacy field readable while new clients select multiple sensors.
    sensor_id: str | None = None
    sensor_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_sensors(self):
        if self.sensor_id and self.sensor_ids and self.sensor_ids != [self.sensor_id]:
            raise ValueError("Use sensor_ids for multiple sensors; conflicting sensor selections are not allowed")
        if self.sensor_id and not self.sensor_ids:
            self.sensor_ids = [self.sensor_id]
        if any(not value.strip() for value in self.sensor_ids) or len(set(self.sensor_ids)) != len(self.sensor_ids):
            raise ValueError("Select each humidity sensor only once")
        self.sensor_id = self.sensor_ids[0] if len(self.sensor_ids) == 1 else None
        return self


class AutomationSpec(SensorSelection):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["humidity_band", "time_window", "cleaning"]
    targets: list[str] = Field(min_length=1)
    lower: float = Field(default=52, ge=0, le=100)
    upper: float = Field(default=55, ge=0, le=100)
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1)
    start: str | None = None
    end: str | None = None
    start_event: Literal["time", "sunrise", "sunset"] = "time"
    end_event: Literal["time", "sunrise", "sunset"] = "time"
    start_offset_minutes: int = Field(default=0, ge=-180, le=180)
    end_offset_minutes: int = Field(default=0, ge=-180, le=180)
    at: str = "10:00"
    min_cycle_seconds: int = Field(default=180, ge=0, le=86400)
    sensor_stale_seconds: int = Field(default=1800, ge=60, le=86400)
    brightness_pct: int | None = Field(default=None, ge=1, le=100)
    enabled: bool = True

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Select each device only once")
        return value

    @field_validator("weekdays")
    @classmethod
    def valid_days(cls, value):
        if any(day not in range(7) for day in value) or len(set(value)) != len(value):
            raise ValueError("Weekdays must be unique numbers from 0 to 6")
        return sorted(value)

    @field_validator("start", "end", "at")
    @classmethod
    def valid_clock(cls, value):
        if value is not None:
            if len(value) != 5 or time.fromisoformat(value).second:
                raise ValueError("Use HH:MM")
        return value

    @model_validator(mode="after")
    def valid_rule(self):
        if self.lower >= self.upper:
            raise ValueError("Lower humidity threshold must be below upper threshold")
        if self.uses_sun:
            if self.kind != "time_window":
                raise ValueError("Sunrise and sunset are available for lighting and power schedules")
            for edge in ("start", "end"):
                event, clock, offset = getattr(self, edge + "_event"), getattr(self, edge), getattr(self, edge + "_offset_minutes")
                if event == "time" and (not clock or offset):
                    raise ValueError("A fixed time needs a clock time and no solar offset")
                if event != "time" and clock is not None:
                    raise ValueError("A solar event cannot also have a fixed clock time")
            if self.start_event == self.end_event and self.start_offset_minutes == self.end_offset_minutes:
                raise ValueError("Select distinct switch-on and switch-off events")
        else:
            if self.start_offset_minutes or self.end_offset_minutes:
                raise ValueError("Offsets require sunrise or sunset")
            if bool(self.start) != bool(self.end) or (self.start and self.start == self.end):
                raise ValueError("Select distinct start and end times")
        if self.kind == "humidity_band" and not self.sensor_ids:
            raise ValueError("Select at least one room humidity sensor")
        if self.kind != "humidity_band" and self.sensor_ids:
            raise ValueError("Only humidity rules use humidity sensors")
        if self.kind == "time_window" and not self.start and not self.uses_sun:
            raise ValueError("Select a time window")
        if self.kind == "cleaning" and self.start:
            raise ValueError("Cleaning uses a start time, not a time window")
        return self

    @property
    def uses_sun(self) -> bool:
        return self.start_event != "time" or self.end_event != "time"


class HomeLocation(StrictModel):
    name: str = Field(default="Home Assistant home", max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    elevation: float = Field(default=0, ge=-500, le=10000)


class SaveAutomation(StrictModel):
    spec: AutomationSpec
    expected_version: int | None = Field(default=None, ge=1)
    controller_id: str | None = None


class ZoneCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Give the zone a name")
        return value.strip()


class ControllerSave(SensorSelection):
    display_name: str = Field(min_length=1, max_length=100)
    zone_id: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Give the controller a name")
        return value.strip()


class DeviceAction(StrictModel):
    action: Literal["turn_on", "turn_off", "start", "pause", "return_to_base", "set_brightness"]
    brightness_pct: int | None = Field(default=None, ge=1, le=100)
