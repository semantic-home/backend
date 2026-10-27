"""Versioned, composable rules. Device references are household device IDs."""
from __future__ import annotations

from datetime import time
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProgramModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class TimeAnchor(ProgramModel):
    event: Literal['clock', 'sunrise', 'sunset', 'elevation', 'elapsed'] = 'clock'
    at: str | None = None
    offset_minutes: int = Field(default=0, ge=-180, le=180)
    elevation: float | None = Field(default=None, ge=-89, le=89)
    direction: Literal['rising', 'setting'] = 'setting'
    minutes: int | None = Field(default=None, ge=1, le=1440)

    @model_validator(mode='after')
    def valid_anchor(self):
        if self.event == 'clock':
            if not self.at or len(self.at) != 5 or time.fromisoformat(self.at).second or self.offset_minutes:
                raise ValueError('A clock anchor needs HH:MM and no offset')
        elif self.at is not None:
            raise ValueError('Only a clock anchor has a clock time')
        if (self.event == 'elevation') != (self.elevation is not None):
            raise ValueError('Choose a sun elevation for an elevation anchor')
        if (self.event == 'elapsed') != (self.minutes is not None):
            raise ValueError('Choose elapsed minutes only for a duration anchor')
        if self.event not in {'sunrise', 'sunset'} and self.offset_minutes:
            raise ValueError('Offsets apply to sunrise and sunset; use a wait step after other events')
        return self


class AnchorTrigger(ProgramModel):
    kind: Literal['anchor'] = 'anchor'
    key: str = Field(default='start',pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    anchor: TimeAnchor

    @model_validator(mode='after')
    def no_relative_start(self):
        if self.anchor.event == 'elapsed':
            raise ValueError('An elapsed duration needs a preceding start')
        return self


class StateTrigger(ProgramModel):
    kind: Literal['state'] = 'state'
    key: str = Field(default='state',pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    device_id: str = Field(min_length=1)
    state: str = Field(min_length=1, max_length=100)


class ThresholdTrigger(ProgramModel):
    kind: Literal['threshold'] = 'threshold'
    key: str = Field(default='threshold',pattern=r'^[a-zA-Z0-9_-]{1,40}$')
    device_id: str = Field(min_length=1)
    comparison: Literal['above', 'below'] = 'above'
    value: float


Trigger = Annotated[Union[AnchorTrigger, StateTrigger, ThresholdTrigger], Field(discriminator='kind')]


class StateCondition(ProgramModel):
    kind: Literal['state'] = 'state'
    device_id: str = Field(min_length=1)
    state: str = Field(min_length=1, max_length=100)


class NumberCondition(ProgramModel):
    kind: Literal['number'] = 'number'
    device_id: str = Field(min_length=1)
    comparison: Literal['above', 'below', 'at_least', 'at_most'] = 'above'
    value: float
    max_age_seconds: int = Field(default=1800, ge=60, le=86400)


class TriggerCondition(ProgramModel):
    kind: Literal['trigger'] = 'trigger'
    key: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,40}$')


class ClockCondition(ProgramModel):
    kind: Literal['clock'] = 'clock'
    start: str
    end: str

    @model_validator(mode='after')
    def valid_window(self):
        if self.start==self.end or any(len(x)!=5 or time.fromisoformat(x).second for x in (self.start,self.end)):
            raise ValueError('Choose distinct HH:MM window edges')
        return self


class ConditionGroup(ProgramModel):
    kind: Literal['all', 'any', 'not'] = 'all'
    terms: list[Union['ConditionGroup', StateCondition, NumberCondition, TriggerCondition, ClockCondition]] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def group_size(self):
        if self.kind == 'not' and len(self.terms) != 1:
            raise ValueError('Not takes exactly one condition or group')
        if self.kind == 'any' and not self.terms:
            raise ValueError('Any needs at least one condition')
        return self


class SetStep(ProgramModel):
    kind: Literal['set'] = 'set'
    action: Literal['turn_on', 'turn_off', 'start', 'pause', 'return_to_base'] = 'turn_on'
    targets: list[str] | None = Field(default=None, min_length=1, max_length=100)
    brightness_pct: int | None = Field(default=None, ge=1, le=100)
    kelvin: int | None = Field(default=None, ge=1000, le=10000)
    transition_seconds: int = Field(default=0, ge=0, le=3600)

    @model_validator(mode='after')
    def fields_match_action(self):
        if self.targets is not None and len(set(self.targets)) != len(self.targets):
            raise ValueError('Select each action target once')
        if self.action != 'turn_on' and (self.brightness_pct is not None or self.kelvin is not None or self.transition_seconds):
            raise ValueError('Light levels and fades require a turn-on action')
        return self


class WaitStep(ProgramModel):
    kind: Literal['wait'] = 'wait'
    seconds: int = Field(default=60, ge=1, le=86400)


class ChooseStep(ProgramModel):
    kind: Literal['choose'] = 'choose'
    condition: ConditionGroup = Field(default_factory=ConditionGroup)
    then: list[Union[SetStep, WaitStep, 'ChooseStep']] = Field(min_length=1, max_length=32)
    otherwise: list[Union[SetStep, WaitStep, 'ChooseStep']] = Field(default_factory=list, max_length=32)


Step = Annotated[Union[SetStep, WaitStep, ChooseStep], Field(discriminator='kind')]


class WindowTiming(ProgramModel):
    mode: Literal['window'] = 'window'
    start: TimeAnchor
    end: TimeAnchor

    @model_validator(mode='after')
    def distinct_edges(self):
        if self.start.event == 'elapsed' or self.start == self.end:
            raise ValueError('Choose a start event and a distinct end')
        return self


class AlwaysTiming(ProgramModel):
    mode: Literal['always'] = 'always'


class EventTiming(ProgramModel):
    mode: Literal['events'] = 'events'
    triggers: list[Trigger] = Field(min_length=1, max_length=16)

    @model_validator(mode='after')
    def unique_keys(self):
        if len({t.key for t in self.triggers})!=len(self.triggers):
            raise ValueError('Each event needs a unique key')
        return self


Timing = Annotated[Union[WindowTiming, AlwaysTiming, EventTiming], Field(discriminator='mode')]


class BandControl(ProgramModel):
    kind: Literal['band'] = 'band'
    sensor_ids: list[str] = Field(min_length=1, max_length=100)
    aggregation: Literal['maximum', 'minimum', 'average'] = 'maximum'
    demand: Literal['above', 'below'] = 'above'
    lower: float = 52
    upper: float = 55
    min_cycle_seconds: int = Field(default=180, ge=0, le=86400)
    max_age_seconds: int = Field(default=1800, ge=60, le=86400)

    @model_validator(mode='after')
    def valid_band(self):
        if self.lower >= self.upper or len(set(self.sensor_ids)) != len(self.sensor_ids):
            raise ValueError('Choose distinct measurements and lower < upper')
        return self


class CurvePoint(ProgramModel):
    position: float
    brightness_pct: int = Field(ge=1, le=100)
    kelvin: int = Field(default=2702, ge=1000, le=10000)


class CurveControl(ProgramModel):
    kind: Literal['curve'] = 'curve'
    driver: Literal['sun_elevation', 'elapsed_minutes', 'measurement'] = 'sun_elevation'
    solar_direction: Literal['rising','setting','any'] = 'any'
    sensor_id: str | None = None
    max_age_seconds: int = Field(default=1800, ge=60, le=86400)
    color_mode: Literal['brightness', 'white'] = 'white'
    points: list[CurvePoint] = Field(min_length=2, max_length=16)
    initial_brightness_pct: int = Field(default=1, ge=1, le=100)
    initial_kelvin: int = Field(default=2702, ge=1000, le=10000)
    startup_minutes: int = Field(default=0, ge=0, le=120)
    respect_manual_off: bool = True

    @model_validator(mode='after')
    def valid_curve(self):
        positions = [p.position for p in self.points]
        if len(set(positions)) != len(positions):
            raise ValueError('Curve points need distinct input values')
        if (self.driver == 'measurement') != bool(self.sensor_id):
            raise ValueError('Choose a measurement only for a measurement-driven curve')
        if self.driver == 'sun_elevation' and any(not -90 <= x <= 90 for x in positions):
            raise ValueError('Sun elevation must be between -90 and 90 degrees')
        if self.driver == 'elapsed_minutes' and any(not 0 <= x <= 1440 for x in positions):
            raise ValueError('Elapsed curve points use 0–1440 minutes')
        return self


class SequenceControl(ProgramModel):
    kind: Literal['sequence'] = 'sequence'
    steps: list[Step] = Field(min_length=1, max_length=32)


class SwitchControl(ProgramModel):
    kind: Literal['switch'] = 'switch'
    brightness_pct: int | None = Field(default=None,ge=1,le=100)
    kelvin: int | None = Field(default=None,ge=1000,le=10000)


Control = Annotated[Union[BandControl, CurveControl, SequenceControl, SwitchControl], Field(discriminator='kind')]


class AutomationProgram(ProgramModel):
    version: Literal[2] = 2
    timing: Timing
    conditions: ConditionGroup = Field(default_factory=ConditionGroup)
    control: Control
    end_action: Literal['hold', 'off'] = 'hold'
    condition_failure: Literal['hold', 'off'] = 'hold'
    max_run_minutes: int = Field(default=720, ge=1, le=1440)

    @model_validator(mode='after')
    def valid_program(self):
        if self.control.kind == 'sequence' and self.timing.mode != 'events':
            raise ValueError('A sequence starts from one or more events')
        if self.control.kind == 'sequence' and self.end_action!='hold':
            raise ValueError('An event sequence uses explicit actions for its ending')
        if self.control.kind == 'curve' and self.timing.mode != 'window':
            raise ValueError('A curve needs a start and end anchor')
        if self.control.kind in {'band','switch'} and self.timing.mode == 'events':
            raise ValueError('Range control runs all day or during a window')
        # Bound recursive work before compiling to HA; no arbitrary scripts.
        nodes = []
        def walk(value, depth=0):
            if depth > 12:
                raise ValueError('Use at most four nested condition/action groups')
            if isinstance(value, dict):
                nodes.append(value)
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        walk(child, depth+1)
            elif isinstance(value, list):
                for child in value:
                    walk(child, depth)
        walk(self.model_dump())
        if len(nodes) > 128:
            raise ValueError('Use at most 128 automation building blocks')
        for node in nodes:
            if node.get('kind')=='trigger' and (self.timing.mode!='events' or node['key'] not in {t.key for t in self.timing.triggers}):
                raise ValueError('A triggered-by condition must reference one of this rule’s events')
        def duration(steps):
            return sum(s.seconds if s.kind=='wait' else max(duration(s.then),duration(s.otherwise)) if s.kind=='choose' else 0 for s in steps)
        if self.control.kind=='sequence' and duration(self.control.steps)>self.max_run_minutes*60:
            raise ValueError('Total waits exceed the maximum sequence duration')
        return self


def input_ids(program: AutomationProgram) -> list[str]:
    result = []
    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'device_id', 'sensor_id'} and child:
                    result.append(child)
                elif key == 'sensor_ids':
                    result.extend(child)
                elif isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(program.model_dump())
    return list(dict.fromkeys(result))
