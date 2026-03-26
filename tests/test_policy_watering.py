from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backend.controller_store.watering import WateringController
from backend.entities_store.entities import EntityStore
from backend.policy.watering import build_watering_rule_preview, evaluate_watering_policy
from backend.rules_store.watering import Action, Condition, Rule, Schedule
from backend.semantic.watering_why import compute_watering_why
from backend.watering_fsm import WateringMode, WateringRuntimeState


def _make_controller() -> WateringController:
    return WateringController(
        controller_id="living_room_main",
        display_name="Living Room",
        actuator_entity_ids=["switch.living_room_pump"],
        agent_id="home1",
        moisture_sensor_entity_ids=["sensor.living_room_moisture_1", "sensor.living_room_moisture_2"],
        moisture_start_below=35.0,
    )


def _make_rule(*conditions: Condition, enabled: bool = True, paused: bool = False, seconds: int = 30) -> Rule:
    return Rule(
        controller_id="living_room_main",
        enabled=enabled,
        paused=paused,
        schedule=Schedule(type="daily", days=[], at="07:00", tz="Europe/Berlin"),
        conditions=list(conditions),
        action=Action(type="watering.start", seconds=seconds),
    )


class WateringPolicyTests(unittest.TestCase):
    def test_denies_when_rule_is_paused(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        decision = evaluate_watering_policy(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%"), paused=True),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
        )

        self.assertEqual(decision.outcome, "deny")
        self.assertEqual(decision.reason_code, "rule_paused")

    def test_allows_when_moisture_is_below_threshold(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})
        entity_store.upsert("sensor.living_room_moisture_2", "40", {})

        decision = evaluate_watering_policy(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
        )

        self.assertEqual(decision.outcome, "allow")
        self.assertEqual(decision.reason_code, "conditions_met")
        self.assertEqual(decision.facts[0].selected_entity_id, "sensor.living_room_moisture_1")
        self.assertTrue(decision.facts[0].comparison_passed)

    def test_denies_when_runtime_is_busy(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        decision = evaluate_watering_policy(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=WateringRuntimeState(mode=WateringMode.ON, run_id="run-1"),
        )

        self.assertEqual(decision.outcome, "deny")
        self.assertEqual(decision.reason_code, "runtime_busy")

    def test_denied_moisture_message_uses_negated_operator_text(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "39", {})

        decision = evaluate_watering_policy(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
        )

        self.assertEqual(decision.outcome, "deny")
        self.assertIn("is not below", decision.message)
        self.assertIn("Condition is not met.", decision.message)

    def test_returns_unknown_for_unsupported_tank_level_condition(self) -> None:
        decision = evaluate_watering_policy(
            rule=_make_rule(Condition(field="tank_level", op=">", value=20, unit="%")),
            controller=_make_controller(),
            entity_store=EntityStore(),
            runtime_state=None,
        )

        self.assertEqual(decision.outcome, "unknown")
        self.assertEqual(decision.reason_code, "condition_field_unsupported")
        self.assertEqual(decision.facts[0].field, "tank_level")

    def test_clamps_effective_seconds_to_failsafe_limit(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        decision = evaluate_watering_policy(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%"), seconds=7200),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
        )

        self.assertEqual(decision.outcome, "allow")
        self.assertEqual(decision.effective_seconds, 3600)
        self.assertIn("failsafe", decision.message.lower())

    def test_preview_includes_summary_and_next_run(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        preview = build_watering_rule_preview(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
            now=datetime(2026, 3, 12, 6, 30, tzinfo=timezone.utc),
        )

        self.assertIn("Start watering for 30 seconds at 07:00 (Europe/Berlin) every day", preview.summary)
        self.assertIsNotNone(preview.next_run_at)
        self.assertEqual(preview.decision.outcome, "allow")

    def test_preview_omits_next_run_for_paused_rule(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        preview = build_watering_rule_preview(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%"), paused=True),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
            now=datetime(2026, 3, 12, 6, 30, tzinfo=timezone.utc),
        )

        self.assertIsNone(preview.next_run_at)
        self.assertEqual(preview.decision.reason_code, "rule_paused")

    def test_preview_reflects_controller_pause(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        preview = build_watering_rule_preview(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
            controller_paused=True,
            now=datetime(2026, 3, 12, 6, 30, tzinfo=timezone.utc),
        )

        self.assertIsNone(preview.next_run_at)
        self.assertEqual(preview.decision.outcome, "deny")
        self.assertEqual(preview.decision.reason_code, "controller_paused")

    def test_preview_skips_visible_next_run_when_skip_next_is_pending(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        preview = build_watering_rule_preview(
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            controller=_make_controller(),
            entity_store=entity_store,
            runtime_state=None,
            skip_next_pending=True,
            now=datetime(2026, 3, 19, 6, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(
            preview.next_run_at,
            datetime(2026, 3, 21, 6, 0, tzinfo=timezone.utc),
        )

    def test_why_uses_rule_policy_when_rule_exists(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "39", {})
        entity_store.upsert("sensor.living_room_moisture_2", "41", {})

        why = compute_watering_why(
            _make_controller(),
            entity_store,
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            runtime_state=None,
        )

        self.assertEqual(why.decision, "skip")
        self.assertEqual(why.reason_code, "conditions_not_met")
        self.assertEqual(why.moisture_value, 39.0)

    def test_why_reports_controller_paused_before_rule_evaluation(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        why = compute_watering_why(
            _make_controller(),
            entity_store,
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            runtime_state=None,
            controller_paused=True,
        )

        self.assertEqual(why.decision, "skip")
        self.assertEqual(why.reason_code, "controller_paused")

    def test_why_does_not_mask_active_runtime_with_controller_paused(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        why = compute_watering_why(
            _make_controller(),
            entity_store,
            rule=_make_rule(Condition(field="moisture", op="<", value=35, unit="%")),
            runtime_state=WateringRuntimeState(mode=WateringMode.ON, run_id="run-1"),
            controller_paused=True,
        )

        self.assertEqual(why.decision, "skip")
        self.assertEqual(why.reason_code, "runtime_busy")

    def test_why_without_rule_reports_no_rule_configured(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        why = compute_watering_why(
            _make_controller(),
            entity_store,
            rule=None,
            runtime_state=None,
        )

        self.assertEqual(why.decision, "unknown")
        self.assertEqual(why.reason_code, "no_rule_configured")
        self.assertIsNone(why.moisture_value)
        self.assertIsNone(why.moisture_start_below)

    def test_why_without_rule_stays_no_rule_configured_when_controller_paused(self) -> None:
        entity_store = EntityStore()
        entity_store.upsert("sensor.living_room_moisture_1", "31", {})

        why = compute_watering_why(
            _make_controller(),
            entity_store,
            rule=None,
            runtime_state=None,
            controller_paused=True,
        )

        self.assertEqual(why.decision, "unknown")
        self.assertEqual(why.reason_code, "no_rule_configured")


if __name__ == "__main__":
    unittest.main()
