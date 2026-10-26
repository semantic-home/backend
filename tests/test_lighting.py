from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from jinja2.nativetypes import NativeEnvironment
from fastapi import HTTPException
from astral import Observer
from astral.sun import elevation

from backend.platform.compiler import compile_automation
from backend.platform.controllers import check_rule_binding
from backend.platform.lighting import ramp_level, ramp_preview
from backend.platform.models import AutomationSpec, DawnRamp
from backend.semantic.capabilities import infer_capabilities

RULE_ID = "01234567-0123-4123-8123-0123456789ab"
ATTRS = {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2702,
         "max_color_temp_kelvin": 6666, "supported_features": 44, "device_id": "physical", "registry_id": "registry"}
TARGET = {"id": "lamp", "entity_id": "light.room", "attributes": ATTRS}
METADATA = {"timezone": "Europe/Berlin", "location": {"name": "Meckenheim", "latitude": 50.625, "longitude": 7.022, "elevation": 180}}


def spec(**patch):
    return AutomationSpec(name="Natural dawn", kind="lighting_ramp", targets=["lamp"], ramp=DawnRamp(), **patch)


class LightingRampTest(unittest.TestCase):
    def render(self, rule=None, trigger="ramp_tick", now="2026-10-26T06:30:00+01:00", angle=-9,
               state="on", physical="physical", rising=True, attrs=None):
        rule, now = rule or spec(), datetime.fromisoformat(now).astimezone(ZoneInfo("Europe/Berlin"))
        config = compile_automation(RULE_ID, rule, [TARGET], [], 1)
        properties = {**ATTRS, "brightness": 255, "color_temp_kelvin": 6500, **(attrs or {})}
        context = {"now": lambda: now, "trigger": SimpleNamespace(id=trigger), "today_at": lambda clock: now.replace(hour=int(clock[:2]), minute=int(clock[3:]), second=0, microsecond=0),
                   "as_timestamp": lambda d: d.timestamp(), "as_datetime": lambda n: datetime.fromtimestamp(n, now.tzinfo), "as_local": lambda d: d.astimezone(now.tzinfo),
                   "state_attr": lambda eid, key: ({"elevation": angle, "rising": rising} if eid == "sun.sun" else properties).get(key),
                   "states": lambda eid: state, "is_state": lambda eid, value: state == value,
                   "device_id": lambda eid: physical, "is_number": lambda v: isinstance(v, (int, float))}
        env = NativeEnvironment()
        render = lambda value: env.from_string(value).render(**context) if isinstance(value, str) else value
        for action in config["actions"]:
            for key, value in action.get("variables", {}).items():
                context[key] = render(value)
        branches = config["actions"][-1]["choose"]
        selected = [all(render(c["value_template"]) for c in b["conditions"]) for b in branches]
        data = {key: render(value) for key, value in branches[-1]["sequence"][0]["data"].items()}
        return config, context, selected, data

    def test_fixed_start_is_always_dim_even_when_daylight_has_already_arrived(self):
        _, context, selected, data = self.render(trigger="ramp_start", now="2026-06-21T06:00:01+02:00", angle=10, state="off")
        self.assertEqual((context["ramp_brightness"], context["ramp_kelvin"]), (1, 2702))
        self.assertEqual(selected, [True])
        self.assertEqual(data, {"brightness_pct": 1, "color_temp_kelvin": 2702, "transition": 0})

    def test_catch_up_fade_and_all_phase_interpolations_match_native_ha_templates(self):
        for angle in [-30, -18, -15, -12, -9, -6, -3, 0, 30]:
            for elapsed in [0, 1, 5, 10, 30]:
                with self.subTest(angle=angle, elapsed=elapsed):
                    now = datetime.fromisoformat("2026-10-26T06:00:00+01:00") + timedelta(minutes=elapsed)
                    _, context, _, data = self.render(now=now.isoformat(), angle=angle)
                    expected = ramp_level(DawnRamp(), angle, elapsed)
                    self.assertEqual(context["ramp_brightness"], expected["brightness_pct"])
                    self.assertEqual(context["ramp_kelvin"], expected["kelvin"])
                    self.assertEqual(data["transition"], 60)
        self.assertEqual(ramp_level(DawnRamp(), 10, 5)["brightness_pct"], 50)

    def test_manual_off_restart_weekday_window_missing_sun_and_identity_guards(self):
        for patch in [{"state": "off"}, {"trigger": "ramp_resume", "state": "off"}, {"physical": "replacement"},
                      {"now": "2026-10-26T05:59:00+01:00"}, {"now": "2026-10-26T12:00:00+01:00"},
                      {"angle": None}, {"angle": -9, "rising": False}]:
            with self.subTest(patch=patch):
                self.assertEqual(self.render(**patch)[2], [False])
        self.assertEqual(self.render(trigger="ramp_resume")[2], [True])
        self.assertEqual(self.render(rule=spec(weekdays=[1]))[2], [False])
        self.assertEqual(self.render(attrs={"brightness": 70, "color_temp_kelvin": 3500}, angle=-9)[2], [False])

    def test_temperature_limits_and_brightness_only_lamps(self):
        rule = spec()
        rule.ramp.astronomical.kelvin = 1000
        rule.ramp.daylight.kelvin = 10000
        self.assertEqual(self.render(rule, angle=-30)[3]["color_temp_kelvin"], 2702)
        self.assertEqual(self.render(rule, angle=10)[3]["color_temp_kelvin"], 6666)
        plain = {"entity_id": "light.plain", "attributes": {"supported_color_modes": ["brightness"]}}
        with self.assertRaises(ValueError):
            compile_automation(RULE_ID, spec(), [plain], [], 1)
        rule.ramp.color_mode = "brightness"
        config = compile_automation(RULE_ID, rule, [plain], [], 1)
        self.assertEqual(set(config["actions"][-1]["choose"][0]["sequence"][0]["data"]), {"brightness_pct"})
        with self.assertRaises(ValueError):
            compile_automation(RULE_ID, rule, [{"entity_id": "switch.plug", "attributes": ATTRS}], [], 1)

    def test_optional_stop_off_runs_without_sun_and_hold_does_not_switch_off(self):
        rule = spec()
        rule.ramp.off_at_end = True
        config, _, selected, _ = self.render(rule, trigger="ramp_stop", now="2026-10-26T12:00:00+01:00", angle=None)
        self.assertEqual(selected, [True, False])
        self.assertEqual(config["actions"][-1]["choose"][0]["sequence"][0]["action"], "light.turn_off")
        self.assertEqual(self.render(trigger="ramp_stop", now="2026-10-26T12:00:00+01:00")[2], [False])

    def test_capabilities_and_multiple_lamps_keep_individual_identity_and_ranges(self):
        self.assertEqual(infer_capabilities("light.room", ATTRS)["color_temp"], {"supported": True, "min_kelvin": 2702, "max_kelvin": 6666})
        self.assertNotIn("color_temp", infer_capabilities("light.rgb", {"supported_color_modes": ["rgb"]}))
        second = {"entity_id": "light.second", "attributes": {**ATTRS, "registry_id": "second", "min_color_temp_kelvin": 3000}}
        config = compile_automation(RULE_ID, spec(), [TARGET, second], [], 1)
        self.assertEqual(config["variables"]["semantic_home_bindings"], {"light.room": "registry", "light.second": "second"})
        self.assertEqual(len(config["actions"]), 5)
        self.assertIn("3000", config["actions"][-1]["choose"][0]["sequence"][0]["data"]["color_temp_kelvin"])

    def test_model_rejects_ambiguous_nonmonotone_and_cross_domain_rules(self):
        for patch in [{"stop_at": "05:00"}, {"start_at": "06:00:00"}, {"startup_minutes": 0}, {"startup_minutes": 121},
                      {"initial_brightness_pct": 10}, {"civil": {"brightness_pct": 2, "kelvin": 4000}},
                      {"daylight": {"brightness_pct": 100, "kelvin": 20000}}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                DawnRamp.model_validate({**DawnRamp().model_dump(), **patch})
        for patch in [{"start": "06:00", "end": "12:00"}, {"brightness_pct": 20}, {"start_event": "sunrise"}, {"sensor_ids": ["humidity"]}]:
            with self.assertRaises(ValueError):
                spec(**patch)
        with self.assertRaises(ValueError):
            AutomationSpec(name="Invalid", kind="time_window", targets=["lamp"], start="06:00", end="12:00", ramp=DawnRamp())
        controller = {"domain": "lighting", "bindings": {"targets": ["lamp"], "sensor_ids": []}}
        check_rule_binding(controller, spec())
        with self.assertRaises(HTTPException) as error:
            check_rule_binding({**controller, "domain": "power"}, spec())
        self.assertEqual(error.exception.status_code, 422)

    def test_location_preview_phases_summer_catch_up_dst_and_polar_days(self):
        for day in ["2026-10-24", "2026-10-25", "2026-06-21"]:
            result = ramp_preview(spec(), METADATA, "HA", "Europe/Berlin", datetime.fromisoformat(day+"T12:00:00+00:00"), True, [TARGET])
            self.assertTrue(result["available"])
            self.assertEqual(result["samples"][0]["brightness_pct"], 1)
            self.assertEqual(result["samples"][0]["kelvin"], 2702)
            self.assertEqual(len(result["phases"]), 4)
            observer = Observer(**{k: v for k, v in METADATA["location"].items() if k != "name"})
            for phase in result["phases"]:
                if phase["at"]:
                    # Astral's position calculation resolves whole seconds;
                    # this is finer than HA's 0.01-degree reported elevation.
                    self.assertAlmostEqual(elevation(observer, datetime.fromisoformat(phase["at"])), phase["elevation"], delta=.005)
            self.assertEqual(result["samples"][-1]["brightness_pct"], 100)
            if day == "2026-06-21":
                self.assertTrue(all(p["before_start"] for p in result["phases"] if p["at"]))
                self.assertEqual(result["samples"][1]["brightness_pct"], 50)
            else:
                self.assertTrue(all(p["at"] for p in result["phases"]))
                self.assertEqual(datetime.fromisoformat(result["start"]).utcoffset(), timedelta(hours=2 if day.endswith("24") else 1))
        missing = ramp_preview(spec(), {}, "HA", "Europe/Berlin", datetime.now().astimezone(), False, [TARGET])
        self.assertFalse(missing["available"])
        polar = {"timezone": "Europe/Oslo", "location": {"latitude": 69.65, "longitude": 18.96}}
        result = ramp_preview(spec(), polar, "HA", "Europe/Oslo", datetime.fromisoformat("2026-06-21T00:00:00+00:00"), False, [TARGET])
        self.assertTrue(result["available"])
        self.assertTrue(all(p["at"] is None for p in result["phases"]))
        self.assertTrue(any("offline" in w for w in result["warnings"]))
        rule = spec()
        rule.ramp.off_at_end = True
        stopped = ramp_preview(rule, METADATA, "HA", "Europe/Berlin", datetime.fromisoformat("2026-10-26T12:00:00+00:00"), True, [TARGET])
        self.assertEqual(stopped["samples"][-1]["brightness_pct"], 0)
        self.assertEqual(stopped["samples"][-2]["brightness_pct"], 100)
        rule.ramp.start_at = "02:30"
        result = ramp_preview(rule, METADATA, "HA", "Europe/Berlin", datetime.fromisoformat("2026-03-29T00:00:00+00:00"), True, [TARGET])
        self.assertFalse(result["available"])


if __name__ == "__main__":
    unittest.main()
