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


class AutomationSpec(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["humidity_band", "time_window", "cleaning"]
    targets: list[str] = Field(min_length=1)
    sensor_id: str | None = None
    lower: float = Field(default=52, ge=0, le=100)
    upper: float = Field(default=55, ge=0, le=100)
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1)
    start: str | None = None
    end: str | None = None
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
        if bool(self.start) != bool(self.end) or (self.start and self.start == self.end):
            raise ValueError("Select distinct start and end times")
        if self.kind == "humidity_band" and not self.sensor_id:
            raise ValueError("Select a room humidity sensor")
        if self.kind != "humidity_band" and self.sensor_id:
            raise ValueError("Only humidity rules use a humidity sensor")
        if self.kind == "time_window" and not self.start:
            raise ValueError("Select a time window")
        if self.kind == "cleaning" and self.start:
            raise ValueError("Cleaning uses a start time, not a time window")
        return self


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


class ControllerSave(StrictModel):
    display_name: str = Field(min_length=1, max_length=100)
    zone_id: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    sensor_id: str | None = None
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
