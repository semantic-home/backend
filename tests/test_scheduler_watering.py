from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backend.controller_store.watering import WateringController, WateringControllerStore
from backend.entities_store.entities import EntityStore
from backend.rules_store.watering import Action, Condition, Rule, RulesStore, Schedule
from backend.scheduler.watering import WateringRuleScheduler


def _make_controller() -> WateringController:
    return WateringController(
        controller_id="living_room_main",
        display_name="Living Room",
        actuator_entity_ids=["switch.living_room_pump"],
        agent_id="home1",
        moisture_sensor_entity_ids=["sensor.living_room_moisture_1"],
        moisture_start_below=35.0,
    )


def _make_rule(
    *,
    schedule_type: str = "daily",
    days: list[str] | None = None,
    at: str = "07:00",
) -> Rule:
    return Rule(
        controller_id="living_room_main",
        enabled=True,
        paused=False,
        schedule=Schedule(type=schedule_type, days=days or [], at=at, tz="Europe/Berlin"),
        conditions=[Condition(field="moisture", op="<", value=35, unit="%")],
        action=Action(type="watering.start", seconds=30),
    )


class FakeRuntime:
    def __init__(self) -> None:
        self.started: list[tuple[str, int]] = []

    def get_state(self, controller_id: str):  # noqa: ANN001
        return None

    async def start(self, controller: WateringController, *, seconds: int) -> str:
        self.started.append((controller.controller_id, seconds))
        return "run-1"


class WateringSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_runtime_when_due_rule_is_allowed(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        runtime = FakeRuntime()
        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=entity_store,
            runtime=runtime,  # type: ignore[arg-type]
        )

        await scheduler.tick_once(now=datetime(2026, 3, 12, 5, 59, tzinfo=timezone.utc))
        await scheduler.tick_once(now=datetime(2026, 3, 12, 6, 0, 5, tzinfo=timezone.utc))

        self.assertEqual(runtime.started, [("living_room_main", 30)])
        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(state["last_outcome"], "allow")
        self.assertEqual(state["last_reason_code"], "conditions_met")
        self.assertIsNotNone(state["next_run_at"])

    async def test_skips_runtime_when_due_rule_is_denied(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "39", {})

        runtime = FakeRuntime()
        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=entity_store,
            runtime=runtime,  # type: ignore[arg-type]
        )

        await scheduler.tick_once(now=datetime(2026, 3, 12, 5, 59, tzinfo=timezone.utc))
        await scheduler.tick_once(now=datetime(2026, 3, 12, 6, 0, 5, tzinfo=timezone.utc))

        self.assertEqual(runtime.started, [])
        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(state["last_outcome"], "deny")
        self.assertEqual(state["last_reason_code"], "conditions_not_met")

    async def test_get_state_view_exposes_next_run_for_existing_rule_before_first_tick(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))

        self.assertEqual(state["controller_id"], "living_room_main")
        self.assertIsNotNone(state["next_run_at"])

    async def test_get_state_view_recomputes_next_run_when_rule_version_changed(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule(schedule_type="weekly", days=["FR"], at="07:00"))

        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        await scheduler.tick_once(now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))

        rules_store.upsert(
            _make_rule(schedule_type="daily", at="12:00"),
            expected_version=1,
        )

        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))

        self.assertIsNotNone(state["next_run_at"])
        self.assertIn("2026-03-14T11:00:00", state["next_run_at"])

    async def test_get_state_view_omits_next_run_for_paused_rule(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(
            Rule(
                controller_id="living_room_main",
                enabled=True,
                paused=True,
                schedule=Schedule(type="daily", days=[], at="07:00", tz="Europe/Berlin"),
                conditions=[Condition(field="moisture", op="<", value=35, unit="%")],
                action=Action(type="watering.start", seconds=30),
            )
        )

        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))

        self.assertIsNone(state["next_run_at"])

    async def test_controller_pause_hides_next_run_and_is_exposed_in_state(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        scheduler.set_paused("living_room_main", True, now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))
        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))

        self.assertIsNone(state["next_run_at"])
        self.assertEqual(state["is_paused"], True)

    async def test_controller_pause_survives_scheduler_tick_without_rule(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        scheduler = WateringRuleScheduler(
            rules_store=RulesStore(),
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        scheduler.set_paused("living_room_main", True, now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))
        await scheduler.tick_once(now=datetime(2026, 3, 14, 8, 1, tzinfo=timezone.utc))
        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 1, tzinfo=timezone.utc))

        self.assertEqual(state["is_paused"], True)
        self.assertIsNone(state["next_run_at"])

    async def test_skip_next_exposes_following_run(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        scheduler.skip_next("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))
        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))

        self.assertEqual(state["skip_next_pending"], True)
        self.assertIn("2026-03-16T06:00:00", state["next_run_at"])

    async def test_skip_next_is_consumed_when_due(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        runtime = FakeRuntime()
        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=entity_store,
            runtime=runtime,  # type: ignore[arg-type]
        )

        scheduler.skip_next("living_room_main", now=datetime(2026, 3, 12, 5, 59, tzinfo=timezone.utc))
        await scheduler.tick_once(now=datetime(2026, 3, 12, 6, 0, 5, tzinfo=timezone.utc))

        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 12, 6, 0, 5, tzinfo=timezone.utc))
        self.assertEqual(runtime.started, [])
        self.assertEqual(state["last_reason_code"], "skip_next_consumed")
        self.assertEqual(state["skip_next_pending"], False)

    async def test_skip_next_cannot_be_requested_twice_before_consumption(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        first_view = scheduler.skip_next("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))
        second_view = scheduler.skip_next("living_room_main", now=datetime(2026, 3, 14, 8, 1, tzinfo=timezone.utc))

        self.assertIsNotNone(first_view)
        self.assertIsNone(second_view)

    async def test_delete_clears_scheduler_state(self) -> None:
        controller_store = WateringControllerStore()
        controller_store.upsert(_make_controller())

        rules_store = RulesStore()
        rules_store.upsert(_make_rule())

        scheduler = WateringRuleScheduler(
            rules_store=rules_store,
            controller_store=controller_store,
            entity_store=EntityStore(),
            runtime=FakeRuntime(),  # type: ignore[arg-type]
        )

        scheduler.set_paused("living_room_main", True, now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))
        scheduler.delete("living_room_main")
        state = scheduler.get_state_view("living_room_main", now=datetime(2026, 3, 14, 8, 0, tzinfo=timezone.utc))

        self.assertEqual(state["controller_id"], "living_room_main")
        self.assertEqual(state["is_paused"], False)


if __name__ == "__main__":
    unittest.main()
