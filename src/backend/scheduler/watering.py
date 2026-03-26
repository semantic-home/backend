from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.controller_store.watering import WateringControllerStore
from backend.entities_store.entities import EntityStore
from backend.rules_store.watering import Schedule
from backend.rules_store.watering import Rule, RulesStore
from backend.watering_fsm import WateringRuntimeManager

logger = logging.getLogger(__name__)

_WEEKDAY_INDEX_BY_CODE: dict[str, int] = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}

DEFAULT_SCHEDULER_TICK_S = 1.0
DEFAULT_SCHEDULER_GRACE_S = 30.0

def advance_next_run(schedule: Schedule, *, scheduled_for: datetime, now: datetime) -> datetime | None:
    next_run_at = compute_next_run_at(schedule, now=scheduled_for)
    while next_run_at is not None and next_run_at <= now:
        next_run_at = compute_next_run_at(schedule, now=next_run_at)
    return next_run_at

def effective_next_run_at(
        *,
        rule: Rule | None,
        raw_next_run_at: datetime | None,
        controller_paused: bool,
        skip_next_pending: bool,
        now: datetime,
) -> datetime | None:
    if controller_paused:
        return None
    if rule is None or not rule.enabled or rule.paused:
        return None
    if raw_next_run_at is None:
        return None
    if not skip_next_pending:
        return raw_next_run_at
    return advance_next_run(rule.schedule, scheduled_for=raw_next_run_at, now=now)




@dataclass
class ScheduledRuleState:
    controller_id: str
    rule_version: int | None = None
    next_run_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    last_outcome: str | None = None
    last_reason_code: str | None = None
    last_error: str | None = None
    controller_paused: bool = False
    skip_next_pending: bool = False


def _parse_clock(value: str) -> tuple[int, int] | None:
    parts = value.split(":")
    if len(parts) != 2:
        return None

    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def compute_next_run_at(schedule: Schedule, *, now: datetime | None = None) -> datetime | None:
    base_now = now if now is not None else datetime.now(timezone.utc)
    if base_now.tzinfo is None:
        base_now = base_now.replace(tzinfo=timezone.utc)

    parsed_clock = _parse_clock(schedule.at)
    if parsed_clock is None:
        return None

    try:
        tz = ZoneInfo(schedule.tz)
    except ZoneInfoNotFoundError:
        return None

    local_now = base_now.astimezone(tz)
    hour, minute = parsed_clock

    if schedule.type == "daily":
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    if schedule.type == "weekly":
        weekday_indexes = {
            _WEEKDAY_INDEX_BY_CODE[day]
            for day in schedule.days
            if day in _WEEKDAY_INDEX_BY_CODE
        }
        if not weekday_indexes:
            return None

        for offset in range(8):
            candidate_date = local_now.date() + timedelta(days=offset)
            candidate = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                hour,
                minute,
                tzinfo=tz,
            )
            if candidate.weekday() not in weekday_indexes:
                continue
            if candidate <= local_now:
                continue
            return candidate.astimezone(timezone.utc)

    return None


def compute_rule_next_run_at(rule: Rule, *, now: datetime | None = None) -> datetime | None:
    if not rule.enabled or rule.paused:
        return None
    return compute_next_run_at(rule.schedule, now=now)


class WateringRuleScheduler:
    def __init__(
        self,
        *,
        rules_store: RulesStore,
        controller_store: WateringControllerStore,
        entity_store: EntityStore,
        runtime: WateringRuntimeManager,
        tick_s: float = DEFAULT_SCHEDULER_TICK_S,
        grace_s: float = DEFAULT_SCHEDULER_GRACE_S,
    ) -> None:
        self._rules_store = rules_store
        self._controller_store = controller_store
        self._entity_store = entity_store
        self._runtime = runtime
        self._tick_s = tick_s
        self._grace_s = grace_s
        self._states: dict[str, ScheduledRuleState] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def clear(self) -> None:
        self._states.clear()

    def delete(self, controller_id: str) -> None:
        self._states.pop(controller_id, None)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(), name="watering-scheduler")

    async def shutdown(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await self._task
        finally:
            self._task = None

    async def tick_once(self, *, now: datetime | None = None) -> None:
        current_now = now if now is not None else datetime.now(timezone.utc)
        if current_now.tzinfo is None:
            current_now = current_now.replace(tzinfo=timezone.utc)

        rules = self._rules_store.list_all()
        active_controller_ids = {rule.controller_id for rule in rules}
        for controller_id in list(self._states.keys()):
            state = self._states[controller_id]
            if controller_id not in active_controller_ids and not state.controller_paused:
                self._states.pop(controller_id, None)

        for rule in rules:
            state = self._sync_state_from_rule(rule, now=current_now)

            if state.next_run_at is None or state.next_run_at > current_now:
                continue

            scheduled_for = state.next_run_at

            if state.controller_paused:
                state.last_evaluated_at = current_now
                state.last_outcome = "skip"
                state.last_reason_code = "controller_paused"
                state.last_error = None
                state.next_run_at = advance_next_run(rule.schedule, scheduled_for=scheduled_for, now=current_now)
                continue

            if state.skip_next_pending:
                state.last_evaluated_at = current_now
                state.last_outcome = "skip"
                state.last_reason_code = "skip_next_consumed"
                state.last_error = None
                state.skip_next_pending = False
                state.next_run_at = advance_next_run(rule.schedule, scheduled_for=scheduled_for, now=current_now)
                continue

            late_by_s = max(0.0, (current_now - scheduled_for).total_seconds())

            if late_by_s > self._grace_s:
                state.last_evaluated_at = current_now
                state.last_outcome = "missed"
                state.last_reason_code = "schedule_slot_missed"
                state.last_error = f"Missed scheduled slot by {late_by_s:.0f}s"
                logger.info(
                    "Skipping missed schedule slot for controller=%s late_by_s=%.1f",
                    rule.controller_id,
                    late_by_s,
                )
            else:
                await self._handle_due_rule(rule=rule, state=state, now=current_now)

            state.next_run_at = advance_next_run(rule.schedule, scheduled_for=scheduled_for, now=current_now)

    def get_state_view(self, controller_id: str, *, now: datetime | None = None) -> dict[str, str | bool | None]:
        current_now = now if now is not None else datetime.now(timezone.utc)
        if current_now.tzinfo is None:
            current_now = current_now.replace(tzinfo=timezone.utc)
        rule = self._rules_store.get(controller_id)
        state = self._states.get(controller_id)

        if rule is not None:
            state = self._sync_state_from_rule(rule, now=current_now)

        if state is None:
            return {
                "controller_id": controller_id,
                "next_run_at": None,
                "last_evaluated_at": None,
                "last_outcome": None,
                "last_reason_code": None,
                "last_error": None,
                "is_paused": False,
                "skip_next_pending": False,
            }

        time_effective_next_run_at = effective_next_run_at(
            rule=rule,
            raw_next_run_at=state.next_run_at,
            controller_paused=state.controller_paused,
            skip_next_pending=state.skip_next_pending,
            now=current_now
        )
        is_paused = state.controller_paused or bool(rule and rule.paused)

        return {
            "controller_id": controller_id,
            "next_run_at": time_effective_next_run_at.isoformat() if time_effective_next_run_at else None,
            "last_evaluated_at": state.last_evaluated_at.isoformat() if state.last_evaluated_at else None,
            "last_outcome": state.last_outcome,
            "last_reason_code": state.last_reason_code,
            "last_error": state.last_error,
            "is_paused": is_paused,
            "skip_next_pending": state.skip_next_pending,
        }

    def set_paused(self, controller_id: str, paused: bool, *, now: datetime | None = None) -> dict[str, str | bool | None]:
        current_now = now if now is not None else datetime.now(timezone.utc)
        if current_now.tzinfo is None:
            current_now = current_now.replace(tzinfo=timezone.utc)

        state = self._states.get(controller_id)
        if state is None:
            state = ScheduledRuleState(controller_id=controller_id)
            self._states[controller_id] = state

        state.controller_paused = paused
        rule = self._rules_store.get(controller_id)
        if rule is not None:
            self._sync_state_from_rule(rule, now=current_now)
        return self.get_state_view(controller_id, now=current_now)

    def is_controller_paused(self, controller_id: str) -> bool:
        state = self._states.get(controller_id)
        return bool(state and state.controller_paused)

    def is_skip_next_pending(self, controller_id: str) -> bool:
        state = self._states.get(controller_id)
        return bool(state and state.skip_next_pending)

    def skip_next(self, controller_id: str, *, now: datetime | None = None) -> dict[str, str | bool | None] | None:
        current_now = now if now is not None else datetime.now(timezone.utc)
        if current_now.tzinfo is None:
            current_now = current_now.replace(tzinfo=timezone.utc)

        rule = self._rules_store.get(controller_id)
        if rule is None:
            return None

        state = self._sync_state_from_rule(rule, now=current_now)
        if state.controller_paused or not rule.enabled or rule.paused or state.next_run_at is None or state.skip_next_pending:
            return None

        state.skip_next_pending = True
        state.last_error = None
        return self.get_state_view(controller_id, now=current_now)

    def _sync_state_from_rule(self, rule: Rule, *, now: datetime) -> ScheduledRuleState:
        state = self._states.get(rule.controller_id)
        if state is None:
            state = ScheduledRuleState(controller_id=rule.controller_id)
            self._states[rule.controller_id] = state

        if state.rule_version != rule.version:
            state.rule_version = rule.version
            state.next_run_at = compute_rule_next_run_at(rule, now=now)
            state.last_error = None

        if state.next_run_at is None:
            state.next_run_at = compute_rule_next_run_at(rule, now=now)

        return state

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            await self.tick_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_s)
            except asyncio.TimeoutError:
                continue

    async def _handle_due_rule(
        self,
        *,
        rule: Rule,
        state: ScheduledRuleState,
        now: datetime,
    ) -> None:
        controller = self._controller_store.get(rule.controller_id)
        state.last_evaluated_at = now

        if controller is None:
            state.last_outcome = "error"
            state.last_reason_code = "missing_controller"
            state.last_error = "No controller found for scheduled rule."
            logger.warning("Scheduled rule references missing controller=%s", rule.controller_id)
            return

        from backend.policy.watering import evaluate_watering_policy

        decision = evaluate_watering_policy(
            rule=rule,
            controller=controller,
            entity_store=self._entity_store,
            runtime_state=self._runtime.get_state(rule.controller_id),
            now=now,
        )
        state.last_outcome = decision.outcome
        state.last_reason_code = decision.reason_code
        state.last_error = None

        if decision.outcome != "allow":
            logger.info(
                "Scheduled rule skipped for controller=%s outcome=%s reason=%s",
                rule.controller_id,
                decision.outcome,
                decision.reason_code,
            )
            return

        seconds = decision.effective_seconds if decision.effective_seconds is not None else rule.action.seconds
        try:
            await self._runtime.start(controller, seconds=seconds)
        except RuntimeError as exc:
            state.last_outcome = "error"
            state.last_reason_code = "runtime_start_failed"
            state.last_error = str(exc)
            logger.warning(
                "Scheduled start failed for controller=%s reason=%s",
                rule.controller_id,
                exc,
            )
