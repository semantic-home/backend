from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from jinja2.nativetypes import NativeEnvironment

from backend.platform.compiler import compile_automation, humidity_decision, window_active
from backend.platform.models import AutomationSpec

RULE_ID = "01234567-0123-4123-8123-0123456789ab"


def rule(**patch):
    return AutomationSpec(name="Humidity with a night cutoff", kind="humidity_band", targets=["plug"],
                          sensor_ids=["kitchen", "living"], **{"start": "06:00", "end": "22:00", **patch})


class HumidityScheduleTest(unittest.TestCase):
    def evaluate(self, spec, clock, values=("60", "54"), age=0, elapsed=1, identity="physical"):
        now = datetime.fromisoformat(clock).astimezone(ZoneInfo("Europe/Berlin"))
        targets = [{"entity_id": "switch.dehumidifier", "attributes": {"device_id": "physical", "registry_id": "plug"}}]
        sensors = [{"entity_id": "sensor."+name, "attributes": {}} for name in ["kitchen", "living"]]
        config = compile_automation(RULE_ID, spec, targets, sensors, 1)
        class States:
            def __init__(self):
                self.records = {"sensor."+name: SimpleNamespace(state=value, last_updated=now-timedelta(seconds=age)) for name, value in zip(["kitchen", "living"], values)}
                self.records["switch.dehumidifier"] = SimpleNamespace(state="on", last_changed=now-timedelta(seconds=elapsed))
            def __call__(self, entity_id): return self.records[entity_id].state
            def __getitem__(self, entity_id): return self.records[entity_id]
        states = States()
        context = {"now": lambda: now, "states": states, "as_timestamp": lambda at: at.timestamp(),
                   "device_id": lambda entity_id: identity, "is_number": lambda value: value not in {"unknown", "unavailable"},
                   "is_state": lambda eid, value: states(eid) == value}
        env = NativeEnvironment()
        branches = config["actions"][0]["choose"]
        decisions = [env.from_string(b["conditions"][1]["value_template"]).render(**context) for b in branches]
        guard = env.from_string(branches[0]["conditions"][-1]["value_template"]).render(**context)
        reason = env.from_string(branches[0]["sequence"][-1]["event_data"]["reason"]).render(**context)
        return config, decisions, reason, guard

    def test_2200_forces_off_above_between_below_and_without_humidity_even_just_switched_on(self):
        spec = rule(min_cycle_seconds=86400)
        for values in [("60", "59"), ("54", "53"), ("51", "50"), ("unknown", "unavailable")]:
            for clock in ["2026-10-26T22:00:00+01:00", "2026-10-26T22:01:00+01:00", "2026-10-27T05:59:59+01:00"]:
                with self.subTest(values=values, clock=clock):
                    _, decisions, reason, guard = self.evaluate(spec, clock, values=values, elapsed=0)
                    self.assertEqual(decisions, [True, False])
                    self.assertEqual(reason, "outside_window")
                    self.assertTrue(guard)
        self.assertEqual(humidity_decision(spec, None, True, False), (False, "outside_window"))
        self.assertEqual(humidity_decision(spec, 60, True, False), (False, "outside_window"))

    def test_window_end_start_and_output_on_have_explicit_local_triggers_for_the_whole_group(self):
        spec = rule()
        targets = [{"entity_id": "switch.one", "attributes": {}}, {"entity_id": "switch.two", "attributes": {}}]
        config = compile_automation(RULE_ID, spec, targets, [{"entity_id": "sensor.kitchen", "attributes": {}}], 2)
        triggers = {t.get("id"): t for t in config["triggers"] if t.get("id")}
        self.assertEqual(triggers["humidity_window_end"], {"trigger": "time", "at": "22:00:00", "id": "humidity_window_end"})
        self.assertEqual(triggers["humidity_window_start"]["at"], "06:00:00")
        self.assertEqual(triggers["humidity_output_on"]["entity_id"], ["switch.one", "switch.two"])
        self.assertEqual(triggers["humidity_output_on"]["to"], "on")
        self.assertEqual(config["mode"], "queued")
        self.assertTrue(any(t["trigger"] == "time_pattern" for t in config["triggers"]))
        self.assertTrue(any(t["trigger"] == "homeassistant" for t in config["triggers"]))
        self.assertEqual(len(config["actions"]), 2)

    def test_control_only_resumes_during_selected_hours_and_physical_identity_still_applies(self):
        spec = rule()
        self.assertEqual(self.evaluate(spec, "2026-10-26T21:59:59+01:00", elapsed=200)[1], [False, True])
        self.assertEqual(self.evaluate(spec, "2026-10-27T06:00:00+01:00", elapsed=200)[1], [False, True])
        self.assertEqual(self.evaluate(spec, "2026-10-27T06:00:00+01:00", elapsed=0)[1], [False, False])
        self.assertEqual(self.evaluate(rule(weekdays=[0]), "2026-10-27T10:00:00+01:00")[1], [True, False])
        self.assertFalse(self.evaluate(spec, "2026-10-26T22:00:00+01:00", identity="replacement")[3])
        self.assertEqual(self.evaluate(spec, "2026-10-26T22:00:00+01:00", age=3600)[2], "outside_window")
        self.assertEqual(self.evaluate(spec, "2026-10-26T21:00:00+01:00", age=3600)[2], "sensor_unavailable")

    def test_all_day_and_overnight_modes_preserve_existing_weekday_and_dst_semantics(self):
        all_day = rule(start=None, end=None)
        config = self.evaluate(all_day, "2026-10-26T22:00:00+01:00", elapsed=200)[0]
        self.assertFalse(any(t["trigger"] == "time" for t in config["triggers"]))
        self.assertTrue(window_active(all_day, datetime.fromisoformat("2026-10-26T22:30:00+01:00"), "Europe/Berlin"))
        overnight = rule(start="22:00", end="06:00", weekdays=[0])
        self.assertEqual(self.evaluate(overnight, "2026-10-27T05:59:59+01:00", elapsed=200)[1], [False, True])
        self.assertEqual(self.evaluate(overnight, "2026-10-27T06:00:00+01:00")[1], [True, False])
        # The end is 22:00 in the home zone on both sides of the DST change.
        for clock in ["2026-10-24T20:00:00+00:00", "2026-10-25T21:00:00+00:00"]:
            self.assertEqual(self.evaluate(rule(), clock)[1], [True, False])


if __name__ == "__main__":
    unittest.main()
