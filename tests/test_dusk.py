from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from astral import Observer
from astral.sun import elevation
from fastapi import HTTPException

import test_lighting as dawn
from backend.platform.lighting import ramp_level, ramp_preview
from backend.platform.models import AutomationSpec, DawnRamp
from backend.platform.service import AutomationService


def dusk_spec(**patch):
    ramp = DawnRamp(direction="dusk", start_at="16:00", stop_at="23:00", initial_brightness_pct=100, off_at_end=True)
    return AutomationSpec.model_validate({**dawn.spec().model_dump(), "name": "Natural dusk", "ramp": ramp.model_dump(), **patch})


class DuskRampTest(unittest.TestCase):
    render = dawn.LightingRampTest.render

    def test_setting_angle_and_start_fade_match_compiled_ha_templates(self):
        rule = dusk_spec()
        for angle in [30, 0, -3, -6, -9, -12, -15, -18, -30]:
            for elapsed in [0, 1, 5, 10, 30]:
                with self.subTest(angle=angle, elapsed=elapsed):
                    now = datetime.fromisoformat("2026-10-26T16:00:00+01:00") + timedelta(minutes=elapsed)
                    _, context, _, data = self.render(rule, now=now.isoformat(), angle=angle, rising=False)
                    expected = ramp_level(rule.ramp, angle, elapsed)
                    self.assertEqual((context["ramp_brightness"], context["ramp_kelvin"]), (expected["brightness_pct"], expected["kelvin"]))
                    self.assertEqual(data["transition"], 60)
        config, context, branches, data = self.render(rule, trigger="ramp_start", now="2026-10-26T16:00:00+01:00", angle=-12, rising=False, state="off")
        self.assertEqual(config["mode"], "queued")
        self.assertEqual(branches, [False, True])
        self.assertEqual((context["ramp_brightness"], context["ramp_kelvin"], data["transition"]), (100, 5500, 0))
        self.assertEqual(ramp_level(rule.ramp, -12, 5), {"brightness_pct": 55, "kelvin": 4250})
        self.assertEqual(ramp_level(rule.ramp, -30, 60), {"brightness_pct": 1, "kelvin": 2702})

    def test_dusk_does_not_replay_dawn_or_override_manual_off_and_identity(self):
        rule = dusk_spec()
        base = {"now": "2026-10-26T18:00:00+01:00", "angle": -9, "rising": False}
        for patch in [{"state": "off"}, {"trigger": "ramp_resume", "state": "off"}, {"physical": "replacement"},
                      {"angle": None}, {"rising": True}, {"now": "2026-10-26T15:59:59+01:00"},
                      {"now": "2026-10-26T23:00:00+01:00"}]:
            with self.subTest(patch=patch):
                self.assertEqual(self.render(rule, **{**base, **patch})[2], [False, False])
        self.assertEqual(self.render(rule, trigger="ramp_resume", **base)[2], [False, True])
        self.assertEqual(self.render(dusk_spec(weekdays=[1]), **base)[2], [False, False])
        self.assertEqual(self.render(rule, trigger="ramp_stop", now="2026-10-26T23:00:00+01:00", angle=None)[2], [True, False])
        rule.ramp.off_at_end = False
        self.assertEqual(self.render(rule, trigger="ramp_stop", now="2026-10-26T23:00:00+01:00")[2], [False])

    def test_setting_preview_has_real_evening_crossings_and_decreasing_curve(self):
        observer = Observer(50.625, 7.022, 180)
        for day in ["2026-10-24", "2026-10-25", "2026-06-21", "2026-12-21"]:
            with self.subTest(day=day):
                result = ramp_preview(dusk_spec(), dawn.METADATA, "HA", "Europe/Berlin", datetime.fromisoformat(day + "T12:00:00+00:00"), True, [dawn.TARGET])
                self.assertTrue(result["available"])
                self.assertEqual([p["key"] for p in result["phases"]], ["daylight", "civil", "nautical", "astronomical"])
                times = [datetime.fromisoformat(p["at"]) for p in result["phases"] if p["at"]]
                self.assertEqual(times, sorted(times))
                self.assertTrue(all(t.hour >= 15 for t in times))
                for phase in result["phases"]:
                    if phase["at"]:
                        at = datetime.fromisoformat(phase["at"])
                        self.assertAlmostEqual(elevation(observer, at), phase["elevation"], delta=.005)
                        self.assertLess(elevation(observer, at + timedelta(minutes=1)), elevation(observer, at))
                levels = [p["brightness_pct"] for p in result["samples"]]
                kelvin = [p["kelvin"] for p in result["samples"]]
                self.assertEqual(levels, sorted(levels, reverse=True))
                self.assertEqual(kelvin, sorted(kelvin, reverse=True))
                self.assertEqual((levels[0], levels[-1]), (100, 0))
                self.assertEqual(datetime.fromisoformat(result["start"]).utcoffset(), timedelta(hours=2 if day in ["2026-10-24", "2026-06-21"] else 1))
        polar = {"timezone": "Europe/Oslo", "location": {"latitude": 69.65, "longitude": 18.96}}
        result = ramp_preview(dusk_spec(), polar, "HA", "Europe/Oslo", datetime.fromisoformat("2026-06-21T12:00:00+00:00"), False, [dawn.TARGET])
        self.assertTrue(all(p["at"] is None for p in result["phases"]))
        self.assertTrue(any("Some dusk" in w for w in result["warnings"]))
        self.assertTrue(any("offline" in w for w in result["warnings"]))

    def test_direction_validation_and_old_dawn_compatibility(self):
        legacy = DawnRamp().model_dump(exclude={"direction"})
        self.assertEqual(DawnRamp.model_validate(legacy).direction, "dawn")
        for patch in [{"direction": "evening"}, {"initial_brightness_pct": 1},
                      {"civil": {"brightness_pct": 5, "kelvin": 4000}}, {"stop_at": "01:00"}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                DawnRamp.model_validate({**dusk_spec().ramp.model_dump(), **patch})

    def test_two_ramps_share_lamps_only_with_disjoint_days_or_separated_windows(self):
        morning, evening = dawn.spec(), dusk_spec()
        rows = [{"deployed_spec": morning.model_dump(), "spec": morning.model_dump(), "requested": False}]
        def query(sql, args):
            if "FROM automations" in sql:
                return rows
            if "FROM devices" in sql:
                return [{"entity_id": "light.room"}]
            return []
        service = AutomationService(SimpleNamespace(all=query), None)
        service.check_conflicts("home", "dusk", evening)
        for start in ["11:59", "12:00"]:
            evening.ramp.start_at = start
            with self.subTest(start=start), self.assertRaises(HTTPException) as conflict:
                service.check_conflicts("home", "dusk", evening)
            self.assertEqual(conflict.exception.status_code, 409)
        evening.ramp.start_at = "12:01"
        service.check_conflicts("home", "dusk", evening)
        # Reverse deployment order and an off-at-end boundary still stay safe.
        rows[0]["deployed_spec"] = evening.model_dump()
        service.check_conflicts("home", "dawn", morning)
        evening.ramp.start_at = "06:00"
        evening.weekdays = [1]
        rows[0]["deployed_spec"] = morning.model_copy(update={"weekdays": [0]}).model_dump()
        service.check_conflicts("home", "dusk", evening)
        # A requested revision must not be hidden by a disjoint deployed spec.
        rows[0].update(requested=True, spec=morning.model_dump())
        with self.assertRaises(HTTPException):
            service.check_conflicts("home", "dusk", evening)
        rows[0].update(requested=False, deployed_spec=AutomationSpec(name="Other", kind="time_window", targets=["lamp"], start="01:00", end="02:00").model_dump())
        with self.assertRaises(HTTPException):
            service.check_conflicts("home", "dusk", dusk_spec())
