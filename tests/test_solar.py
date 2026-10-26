from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sunrise, sunset
from jinja2.nativetypes import NativeEnvironment

from backend.platform.compiler import compile_automation
from backend.platform.models import AutomationSpec, HomeLocation
from backend.platform.solar import event_time, solar_preview

LOCATION = {"name": "Meckenheim", "latitude": 50.625, "longitude": 7.022, "elevation": 180}
METADATA = {"timezone": "Europe/Berlin", "location": LOCATION}
RULE_ID = "01234567-0123-4123-8123-0123456789ab"


def spec(**patch):
    return AutomationSpec.model_validate({"name": "Evening lights", "kind": "time_window", "targets": ["lamp"],
        "start_event": "sunset", "start_offset_minutes": -30, "end": "23:30", **patch})


class SolarScheduleTest(unittest.TestCase):
    def test_model_preserves_old_fixed_windows_and_rejects_ambiguous_solar_inputs(self):
        old = AutomationSpec(name="Old", kind="time_window", targets=["lamp"], start="08:00", end="22:00")
        self.assertFalse(old.uses_sun)
        self.assertEqual(AutomationSpec.model_validate(spec().model_dump()), spec())
        for patch in [{"start": "20:00"}, {"start_offset_minutes": 181}, {"start_offset_minutes": -181},
                      {"end_offset_minutes": 20}, {"end": None}, {"kind": "cleaning"},
                      {"start_event": "dawn"}, {"end_event": "sunset", "end": None, "end_offset_minutes": -30}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                spec(**patch)
        with self.assertRaises(ValueError):
            spec(kind="humidity_band", sensor_ids=["humidity"])

    def test_compiles_real_native_triggers_with_offsets_and_brightness(self):
        rule = spec(end_event="sunrise", end=None, end_offset_minutes=20, brightness_pct=40)
        config = compile_automation(RULE_ID, rule, [{"entity_id": "light.room", "attributes": {"device_id": "physical", "registry_id": "registry"}}], [], 2)
        self.assertEqual(config["triggers"], [{"trigger": "sun", "event": "sunset", "offset": -1800, "id": "schedule_on"},
                                              {"trigger": "sun", "event": "sunrise", "offset": 1200, "id": "schedule_off"}])
        self.assertEqual(config["variables"]["semantic_home_bindings"], {"light.room": "registry"})
        branches = config["actions"][0]["choose"]
        self.assertEqual(branches[1]["sequence"][0]["data"], {"brightness_pct": 40})
        self.assertIn("physical", branches[1]["conditions"][-1]["value_template"])
        fixed_end = compile_automation(RULE_ID, spec(), [{"entity_id": "switch.lamp", "attributes": {}}], [], 1)
        self.assertEqual(fixed_end["triggers"][1], {"trigger": "time", "at": "23:30:00", "id": "schedule_off"})

    def test_trigger_actions_use_anchor_weekday_across_midnight_and_off_runs_daily(self):
        rule = spec(weekdays=[0], start_offset_minutes=180)
        config = compile_automation(RULE_ID, rule, [{"entity_id": "switch.lamp", "attributes": {}}], [], 1)
        expressions = [branch["conditions"][1]["value_template"] for branch in config["actions"][0]["choose"]]
        env, zone = NativeEnvironment(), ZoneInfo("Europe/Berlin")
        for value, trigger_id, expected in [("2026-10-20T00:30:00+02:00", "schedule_on", [False, True]),
                                              ("2026-10-21T00:30:00+02:00", "schedule_on", [False, False]),
                                              ("2026-10-21T23:30:00+02:00", "schedule_off", [True, False])]:
            now = datetime.fromisoformat(value)
            context = {"now": lambda: now, "trigger": SimpleNamespace(id=trigger_id), "as_timestamp": lambda d: d.timestamp(),
                       "as_datetime": lambda t: datetime.fromtimestamp(t, timezone.utc), "as_local": lambda d: d.astimezone(zone)}
            self.assertEqual([env.from_string(e).render(**context) for e in expressions], expected)

    def test_preview_matches_astral_includes_offsets_and_never_uses_browser_timezone(self):
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        result = solar_preview(spec(), METADATA, "Local HA", "Europe/Berlin", now, True)
        self.assertTrue(result["available"])
        self.assertEqual(result["location_name"], "Meckenheim")
        zone = ZoneInfo("Europe/Berlin")
        expected = sunset(Observer(LOCATION["latitude"], LOCATION["longitude"], LOCATION["elevation"]), now.date(), tzinfo=zone)
        actual = datetime.fromisoformat(next(e["at"] for e in result["events"] if e["action"] == "on"))
        self.assertEqual(actual, expected - timedelta(minutes=30))
        self.assertEqual(actual.utcoffset(), timedelta(hours=1))
        self.assertTrue(all(datetime.fromisoformat(e["at"]).timestamp() > now.timestamp() for e in result["events"]))

    def test_sunrise_to_sunset_and_fixed_start_to_solar_end(self):
        now = datetime(2026, 10, 26, 0, tzinfo=timezone.utc)
        for rule in [spec(start_event="sunrise", start_offset_minutes=0, end_event="sunset", end=None),
                     spec(start_event="time", start="18:00", start_offset_minutes=0, end_event="sunrise", end=None)]:
            result = solar_preview(rule, METADATA, "HA", "Europe/Berlin", now, True)
            self.assertEqual({e["action"] for e in result["events"]}, {"on", "off"})
            timestamps = [datetime.fromisoformat(e["at"]).timestamp() for e in result["events"]]
            self.assertEqual(timestamps, sorted(timestamps))

    def test_dst_changes_local_offsets_without_freezing_solar_times(self):
        zone, location = ZoneInfo("Europe/Berlin"), HomeLocation(**LOCATION)
        before = event_time("sunrise", None, -30, date(2026, 10, 24), zone, location)
        after = event_time("sunrise", None, -30, date(2026, 10, 25), zone, location)
        self.assertEqual((before.utcoffset(), after.utcoffset()), (timedelta(hours=2), timedelta(hours=1)))
        self.assertNotEqual(before.time(), after.time())
        self.assertIsNone(event_time("time", "02:30", 0, date(2026, 3, 29), zone, location))
        result = solar_preview(spec(end="02:30"), METADATA, "HA", "Europe/Berlin", datetime(2026, 10, 25, 0, tzinfo=timezone.utc), True)
        repeats = [e for e in result["events"] if e["action"] == "off" and e["anchor_date"] == "2026-10-25"]
        self.assertEqual(len(repeats), 2)
        self.assertNotEqual(datetime.fromisoformat(repeats[0]["at"]).utcoffset(), datetime.fromisoformat(repeats[1]["at"]).utcoffset())

    def test_missing_location_mismatch_offline_and_polar_dates_are_explicit(self):
        now = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)
        missing = solar_preview(spec(), {}, "HA", "Europe/Berlin", now, True)
        self.assertFalse(missing["available"])
        self.assertIn("not reported", missing["warnings"][0])
        mismatch = solar_preview(spec(), {**METADATA, "timezone": "UTC"}, "HA", "Europe/Berlin", now, True)
        self.assertFalse(mismatch["available"])
        offline = solar_preview(spec(), METADATA, "HA", "Europe/Berlin", now, False)
        self.assertIn("offline", offline["warnings"][0])
        polar = {"timezone": "Europe/Oslo", "location": {"name": "Tromso", "latitude": 69.65, "longitude": 18.96}}
        result = solar_preview(spec(), polar, "HA", "Europe/Oslo", now, True)
        self.assertIsNone(result["sunrise"])
        self.assertTrue(result["warnings"])
        self.assertFalse(any(e["action"] == "on" for e in result["events"]))

    def test_selected_weekdays_apply_to_event_date_before_offset(self):
        result = solar_preview(spec(weekdays=[0]), METADATA, "HA", "Europe/Berlin", datetime(2026, 10, 26, 12, tzinfo=timezone.utc), True)
        on = [e for e in result["events"] if e["action"] == "on"]
        self.assertTrue(on)
        self.assertTrue(all(date.fromisoformat(e["anchor_date"]).weekday() == 0 for e in on))
        self.assertTrue(any(date.fromisoformat(e["anchor_date"]).weekday() != 0 for e in result["events"] if e["action"] == "off"))


if __name__ == "__main__":
    unittest.main()
