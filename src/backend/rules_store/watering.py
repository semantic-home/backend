from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict, Optional

from pydantic import BaseModel



class Schedule(BaseModel):
    type: str
    days: list[str]
    at: str
    tz: str


class Condition(BaseModel):
    field: str
    op: str
    value: int | float | str
    unit: Optional[str] = None


class Action(BaseModel):
    type: str
    seconds: int


@dataclass
class Rule:
    controller_id: str
    enabled: bool
    paused: bool
    schedule: Schedule
    conditions: list[Condition]
    action: Action
    version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RuleNotFoundError(KeyError):
    pass


class RuleAlreadyExistsError(ValueError):
    pass


class RuleVersionConflictError(ValueError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RulesStore:
    def __init__(self) -> None:
        self._rules: Dict[str, Rule] = {}

    def clear(self) -> None:
        self._rules.clear()

    def create(self, rule: Rule) -> Rule:
        if rule.controller_id in self._rules:
            raise RuleAlreadyExistsError(f"Rules already exist for controller_id={rule.controller_id}")
        now = _utc_now()
        stored = replace(
            rule,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._rules[stored.controller_id] = stored
        return stored

    def update(self, rule: Rule, *, expected_version: int | None = None) -> Rule:
        current = self._rules.get(rule.controller_id)
        if current is None:
            raise RuleNotFoundError(f"Unknown controller_id={rule.controller_id}")

        if expected_version is not None and current.version != expected_version:
            raise RuleVersionConflictError(
                f"Version conflict for controller_id={rule.controller_id}: "
                f"expected={expected_version}, current={current.version}"
            )

        stored = replace(
            rule,
            version=current.version + 1,
            created_at=current.created_at,
            updated_at=_utc_now(),
        )
        self._rules[stored.controller_id] = stored
        return stored

    def upsert(self, rule: Rule, *, expected_version: int | None = None) -> Rule:
        if rule.controller_id in self._rules:
            return self.update(rule, expected_version=expected_version)
        return self.create(rule)

    def get(self, controller_id: str) -> Optional[Rule]:
        return self._rules.get(controller_id)

    def list_all(self) -> list[Rule]:
        return list(self._rules.values())

    def delete(self, controller_id: str) -> None:
        self._rules.pop(controller_id, None)
