from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.agent_hub.agent_hub import AgentHub
from backend.platform.compiler import compile_automation, humidity_decision, window_active
from backend.platform.database import Database
from backend.platform.models import AutomationSpec

URL = os.environ.get("TEST_DATABASE_URL", "")
logging.getLogger("httpx").setLevel(logging.WARNING)


class RuleModelTest(unittest.TestCase):
    def rule(self, **kwargs):
        return AutomationSpec(name="Keller", kind="humidity_band", targets=["plug"], sensor_id="humidity", **kwargs)

    def test_hysteresis(self):
        rule = self.rule()
        state = False
        results = []
        for value in [54, 55, 54, 52, 54, 56]:
            state, _ = humidity_decision(rule, value, state, True)
            results.append(state)
        self.assertEqual(results, [False, True, True, False, False, True])
        self.assertEqual(humidity_decision(rule, None, True, True), (False, "sensor_unavailable"))
        self.assertEqual(humidity_decision(rule, 60, True, False), (False, "outside_window"))

    def test_overnight_window_uses_start_weekday(self):
        rule = self.rule(weekdays=[0], start="22:00", end="06:00")
        for value, expected in [("2026-10-19T20:30:00+00:00", True), ("2026-10-20T02:00:00+00:00", True),
                                ("2026-10-20T05:00:00+00:00", False), ("2026-10-20T21:00:00+00:00", False)]:
            self.assertEqual(window_active(rule, datetime.fromisoformat(value), "Europe/Berlin"), expected)

    def test_regional_clock_and_dst(self):
        rule = self.rule(start="01:00", end="03:00")
        now = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
        self.assertTrue(window_active(rule, now, "Europe/Berlin"))
        self.assertFalse(window_active(rule, now, "America/New_York"))

    def test_invalid_model_inputs(self):
        for patch in [{"lower": 60, "upper": 55}, {"weekdays": []}, {"weekdays": [7]}, {"start": "08:00"},
                      {"start": "08:00", "end": "08:00"}, {"upper": float("nan")}, {"targets": ["x", "x"]}]:
            with self.assertRaises(ValueError):
                self.rule(**patch) if "targets" not in patch else AutomationSpec(name="x", kind="time_window", start="08:00", end="22:00", **patch)

    def test_generated_templates_parse_and_keep_local_state(self):
        from jinja2 import Environment
        rule = self.rule()
        target = {"entity_id": "switch.keller", "attributes": {}}
        config = compile_automation("01234567-0123-4123-8123-0123456789ab", rule, [target], {"entity_id": "sensor.humidity", "attributes": {}}, 3)
        self.assertEqual(config["id"], "semantic_home_012345670123412381230123456789ab")
        env = Environment()
        for branch in config["actions"][0]["choose"]:
            env.parse(branch["conditions"][1]["value_template"])
        self.assertTrue(any(t["trigger"] == "homeassistant" for t in config["triggers"]))
        self.assertTrue(any(t["trigger"] == "time_pattern" for t in config["triggers"]))
        cleaning = rule.model_copy(update={"kind": "cleaning", "sensor_id": None})
        generated = compile_automation("01234567-0123-4123-8123-0123456789ab", cleaning, [{"entity_id": "vacuum.robot", "attributes": {}}], None, 1)
        self.assertEqual([t["trigger"] for t in generated["triggers"]], ["time"])

    def test_generated_humidity_conditions_match_real_decisions_and_cycle_limit(self):
        from jinja2.nativetypes import NativeEnvironment
        from datetime import timedelta
        rule = self.rule()
        config = compile_automation("01234567-0123-4123-8123-0123456789ab", rule,
            [{"entity_id": "switch.keller", "attributes": {}}], {"entity_id": "sensor.humidity", "attributes": {}}, 1)
        env = NativeEnvironment()
        now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
        class States:
            def __init__(self, value, stale, elapsed):
                self.values = {"sensor.humidity": SimpleNamespace(state=value, last_updated=now-timedelta(seconds=stale)),
                               "switch.keller": SimpleNamespace(state="off", last_changed=now-timedelta(seconds=elapsed))}
            def __call__(self, entity_id): return self.values[entity_id].state
            def __getitem__(self, entity_id): return self.values[entity_id]
        for value, stale, elapsed, off, on in [("55", 0, 200, False, True), ("54", 0, 200, False, False),
            ("52", 0, 200, True, False), ("55", 0, 1, False, False), ("unknown", 0, 1, True, False), ("55", 1900, 1, True, False)]:
            context = {"states": States(value, stale, elapsed), "now": lambda: now,
                       "as_timestamp": lambda value: value.timestamp(), "is_number": lambda raw: raw not in {"unknown", "unavailable"}}
            actual = [env.from_string(b["conditions"][1]["value_template"]).render(**context) for b in config["actions"][0]["choose"]]
            self.assertEqual(actual, [off, on], (value, stale, elapsed))


class HubTest(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_agent_and_replaced_connection_cannot_ack(self):
        class Socket:
            def __init__(self): self.payload = None
            async def send_text(self, raw): self.payload = json.loads(raw)
            async def close(self, **kwargs): pass
        hub, first, other, replacement = AgentHub(), Socket(), Socket(), Socket()
        await hub.register("a", first)
        await hub.register("b", other)
        task = asyncio.create_task(hub.send_command_and_wait_ack("a", "test", {}, 1))
        await asyncio.sleep(0)
        ack = {"type": "ack", "id": first.payload["id"], "ok": True}
        await hub.handle_incoming(ack, agent_id="b", ws=other)
        self.assertFalse(task.done())
        await hub.handle_incoming(ack, agent_id="a", ws=first)
        self.assertTrue((await task).ok)
        await hub.register("a", replacement)
        await hub.unregister("a", ws=first)
        self.assertTrue(hub.is_current("a", replacement))


@unittest.skipUnless(URL, "Set TEST_DATABASE_URL to an isolated PostgreSQL test database")
class HouseholdIntegrationTest(unittest.TestCase):
    def setUp(self):
        from backend.__main__ import create_app
        if not URL.rsplit("/", 1)[-1].startswith("semantic_home_test"):
            raise RuntimeError("Refusing to clear a non-test database")
        db = Database(URL)
        db.open()
        db.execute("TRUNCATE users,homes,auth_attempts RESTART IDENTITY CASCADE")
        db.close()
        self.app = create_app(URL)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.alice, self.ah = self.account("alice@example.test")
        self.bob, self.bh = self.account("bob@example.test")

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def account(self, email):
        response = self.client.post("/api/v1/auth/register", json={"email": email, "password": "a-long-test-password"})
        self.assertEqual(response.status_code, 200, response.text)
        headers = {"cookie": "semantic_session=" + response.cookies["semantic_session"]}
        response = self.client.post("/api/v1/homes", json={"name": email, "timezone": "Europe/Berlin"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        headers["x-semantic-home-household"] = response.json()["id"]
        return headers, response.json()["id"]

    def connection(self, headers):
        response = self.client.post("/api/v1/connections", json={"name": "Local HA"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ingest(self, headers, connection, entity_id="switch.keller", state="off", attrs=None):
        from backend.schemas.ha_messages import HAEntityState
        self.app.state.db.ingest(headers["x-semantic-home-household"], connection["id"], [
            HAEntityState(entity_id=entity_id, state=state, attributes=attrs or {"friendly_name": "Keller"})])
        return self.client.get("/api/v1/devices", headers=headers).json()["items"]

    def spec(self, device_id, **patch):
        return {"name": "Abendlicht", "kind": "time_window", "targets": [device_id], "start": "18:00", "end": "23:00", **patch}

    def test_same_entity_ids_and_all_legacy_routes_are_isolated(self):
        ac, bc = self.connection(self.alice), self.connection(self.bob)
        ad = self.ingest(self.alice, ac, state="on")[0]
        bd = self.ingest(self.bob, bc, state="off")[0]
        self.assertNotEqual(ad["id"], bd["id"])
        self.assertEqual(self.client.get("/api/v1/devices", headers=self.alice).json()["items"][0]["state"], "on")
        self.assertEqual(self.client.get("/api/v1/domains/power/entities", headers=self.bob).json()[0]["state"], "off")
        self.assertEqual(self.client.get("/api/v1/devices/" + ad["id"], headers=self.bob).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/devices", headers={**self.bob, "x-semantic-home-household": self.ah}).status_code, 404)
        result = self.client.post("/api/v1/automations", headers=self.bob, json={"spec": self.spec(ad["id"])})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.client.get("/api/v1/agent/list_agents", headers=self.bob).json(), {"agents": []})

    def test_anonymous_origin_and_foreign_agent_rejected(self):
        self.client.cookies.clear()
        for path in ["/devices", "/automations", "/domains/watering/controllers", "/semantic/classifications", "/agent/list_agents"]:
            self.assertEqual(self.client.get("/api/v1" + path).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/homes", headers={**self.alice, "origin": "https://attacker.example"}, json={"name": "x"}).status_code, 403)
        ac = self.connection(self.alice)
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/v1/ws/agent/" + ac["id"], headers={"Authorization": "Bearer invalid"}): pass

    def test_login_rejects_wrong_password_and_logout_revokes_session(self):
        self.client.cookies.clear()
        for email in ("alice@example.test", "missing@example.test"):
            self.assertEqual(self.client.post("/api/v1/auth/login", json={"email": email, "password": "wrong-long-password"}).status_code, 401)
        result = self.client.post("/api/v1/auth/login", json={"email": "alice@example.test", "password": "a-long-test-password"})
        self.assertEqual(result.status_code, 200)
        credential = {"cookie": "semantic_session=" + result.cookies["semantic_session"]}
        self.assertEqual(self.client.get("/api/v1/devices", headers=credential).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/logout", headers=credential).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/devices", headers=credential).status_code, 401)

    def test_readonly_member_and_multi_home_membership(self):
        bob_id = self.app.state.db.one("SELECT id FROM users WHERE email='bob@example.test'")["id"]
        self.assertEqual(self.client.post("/api/v1/members", headers=self.alice, json={"email": "bob@example.test", "role": "viewer"}).status_code, 422)
        response = self.client.post("/api/v1/members", headers=self.alice, json={"user_id": bob_id, "role": "viewer"})
        self.assertEqual(response.status_code, 200)
        bob = {**self.bob, "x-semantic-home-household": self.ah}
        self.assertEqual(self.client.get("/api/v1/connections", headers=bob).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/connections", headers=bob, json={"name": "x"}).status_code, 403)
        self.assertEqual(len(self.client.get("/api/v1/homes", headers=self.bob).json()), 2)
        bob_id = self.app.state.db.one("SELECT id FROM users WHERE email='bob@example.test'")["id"]
        self.assertEqual(self.client.delete("/api/v1/members/" + bob_id, headers=self.alice).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/connections", headers=bob).status_code, 404)

    def test_individual_agent_credential_revocation_and_rotation(self):
        from starlette.websockets import WebSocketDisconnect
        ac = self.connection(self.alice)
        self.assertEqual(self.client.post("/api/v1/connections/" + ac["id"] + "/rotate", headers=self.bob, json={}).status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/connections/" + ac["id"], headers=self.alice).status_code, 200)
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/api/v1/ws/agent/" + ac["id"], headers={"Authorization": "Bearer " + ac["token"]}): pass
        renewed = self.client.post("/api/v1/connections/" + ac["id"] + "/rotate", headers=self.alice, json={}).json()
        self.assertNotEqual(renewed["token"], ac["token"])
        with self.client.websocket_connect("/api/v1/ws/agent/" + ac["id"], headers={"Authorization": "Bearer " + renewed["token"]}) as ws:
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json()["type"], "pong")

    def test_save_preview_version_conflict_and_postgres_persistence(self):
        ac = self.connection(self.alice)
        device = self.ingest(self.alice, ac)[0]
        spec = self.spec(device["id"])
        response = self.client.post("/api/v1/automations/preview", headers=self.alice, json=spec)
        self.assertEqual(response.status_code, 200, response.text)
        rule = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": spec}).json()
        result = self.client.put("/api/v1/automations/" + rule["id"], headers=self.alice, json={"spec": spec, "expected_version": 7})
        self.assertEqual(result.status_code, 409)
        result = self.client.post("/api/v1/automations/" + rule["id"] + "/deploy", headers=self.alice, json={})
        self.assertEqual(result.json()["status"], "pending")
        self.assertTrue(result.json()["requested"])
        separate = Database(URL)
        separate.open()
        self.assertEqual(separate.rule(self.ah, rule["id"])["spec"]["name"], "Abendlicht")
        separate.close()
        self.assertEqual(self.client.get("/api/v1/automations", headers=self.bob).json(), [])

    def test_local_deployment_requires_source_bound_ack_and_pause(self):
        ac = self.connection(self.alice)
        with self.client.websocket_connect("/api/v1/ws/agent/" + ac["id"], headers={"Authorization": "Bearer " + ac["token"]}) as ws:
            ws.send_json({"type": "metadata", "timezone": "Europe/Berlin", "ha_version": "2026.9.4"})
            ws.send_json({"type": "entities_snapshot", "entities": [{"entity_id": "switch.keller", "state": "off", "attributes": {}}]})
            for _ in range(20):
                devices = self.client.get("/api/v1/devices", headers=self.alice).json()["items"]
                if devices: break
                time.sleep(.01)
            spec = self.spec(devices[0]["id"])
            rule = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": spec}).json()
            with ThreadPoolExecutor() as executor:
                pending = executor.submit(self.client.post, "/api/v1/automations/" + rule["id"] + "/deploy", headers=self.alice, json={})
                command = ws.receive_json()
                self.assertEqual(command["name"], "automation.apply")
                self.assertTrue(command["args"]["config_id"].startswith("semantic_home_"))
                ws.send_json({"type": "ack", "id": command["id"], "ok": True, "result": {"state": "on"}})
                self.assertEqual(pending.result(timeout=5).json()["status"], "active")
                edit = self.client.put("/api/v1/automations/" + rule["id"], headers=self.alice, json={"spec": spec, "expected_version": 1})
                self.assertEqual(edit.status_code, 409)
                action = self.client.post("/api/v1/devices/" + devices[0]["id"] + "/actions", headers=self.alice, json={"action": "turn_on"})
                self.assertEqual(action.status_code, 409)
                pending = executor.submit(self.client.post, "/api/v1/automations/" + rule["id"] + "/pause", headers=self.alice, json={})
                command = ws.receive_json()
                self.assertFalse(command["args"]["config"]["initial_state"])
                ws.send_json({"type": "ack", "id": command["id"], "ok": True, "result": {"state": "off"}})
                self.assertEqual(pending.result(timeout=5).json()["status"], "paused")

    def test_paginated_inventory_and_room_humidity_validation(self):
        ac = self.connection(self.alice)
        from backend.schemas.ha_messages import HAEntityState
        self.app.state.db.ingest(self.ah, ac["id"], [HAEntityState(entity_id=f"switch.plug_{n}", state="off") for n in range(127)])
        page = self.client.get("/api/v1/devices?limit=50&offset=100", headers=self.alice).json()
        self.assertEqual(page["total"], 127)
        self.assertEqual(len(page["items"]), 27)
        soil = self.ingest(self.alice, ac, "sensor.soil", "50", {"device_class": "moisture", "unit_of_measurement": "%"})
        soil_id = next(d["id"] for d in soil if d["entity_id"] == "sensor.soil") if any(d["entity_id"] == "sensor.soil" for d in soil) else self.app.state.db.one("SELECT id FROM devices WHERE home_id=%s AND entity_id='sensor.soil'", (self.ah,))["id"]
        spec = {"name": "Humidity", "kind": "humidity_band", "targets": [page["items"][0]["id"]], "sensor_id": soil_id}
        self.assertEqual(self.client.post("/api/v1/automations", headers=self.alice, json={"spec": spec}).status_code, 422)
        humidity = self.client.get("/api/v1/devices?capability=humidity", headers=self.alice)
        self.assertEqual(humidity.status_code, 200, humidity.text)
        self.assertEqual(humidity.json()["total"], 0)
        self.ingest(self.alice, ac, "sensor.room", "55", {"device_class": "humidity", "unit_of_measurement": "%"})
        self.assertEqual(self.client.get("/api/v1/devices?capability=humidity", headers=self.alice).json()["total"], 1)

    def test_device_rename_retains_binding_and_replacement_gets_new_identity(self):
        ac = self.connection(self.alice)
        original = self.ingest(self.alice, ac, attrs={"registry_id": "physical-original"})[0]
        renamed = self.ingest(self.alice, ac, "switch.new_name", attrs={"registry_id": "physical-original"})[0]
        self.assertEqual(original["id"], renamed["id"])
        self.ingest(self.alice, ac, "switch.new_name", attrs={"registry_id": "physical-replacement"})
        row = self.app.state.db.one("SELECT * FROM devices WHERE home_id=%s AND entity_id='switch.new_name'", (self.ah,))
        self.assertNotEqual(row["id"], original["id"])
        self.assertFalse(self.app.state.db.device(self.ah, original["id"])["available"])

    def test_real_app_restart_preserves_session_rules_and_watering_pause(self):
        from backend.__main__ import create_app
        ac = self.connection(self.alice)
        device = self.ingest(self.alice, ac)[0]
        created = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": self.spec(device["id"])}).json()
        controller = self.client.post("/api/v1/domains/watering/controllers", headers=self.alice, json={
            "controller_id": "plant", "display_name": "Plant", "agent_id": ac["id"], "actuator_entity_ids": ["switch.keller"],
            "zone": {"zone_id": "own_room", "display_name": "Own room"}})
        self.assertEqual(controller.status_code, 200, controller.text)
        response = self.client.put("/api/v1/domains/watering/controllers/plant/rule", headers=self.alice, json={
            "schedule": {"type": "daily", "days": [], "at": "06:00", "tz": "Europe/Berlin"}, "action": {"type": "watering.start", "seconds": 10}})
        self.assertEqual(response.status_code, 200, response.text)
        self.client.post("/api/v1/domains/watering/controllers/plant/pause", headers=self.alice, json={}).raise_for_status()
        self.context.__exit__(None, None, None)
        self.app = create_app(URL)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.alice).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/automations", headers=self.alice).json()[0]["id"], created["id"])
        self.assertEqual(self.client.get("/api/v1/domains/watering/controllers", headers=self.alice).json()[0]["controller_id"], "plant")
        self.assertTrue(self.client.get("/api/v1/domains/watering/controllers/plant/next", headers=self.alice).json()["is_paused"])
        self.assertTrue(any(room["id"] == "own_room" for room in self.client.get("/api/v1/rooms", headers=self.alice).json()))
        self.assertEqual(self.client.get("/api/v1/domains/watering/controllers", headers=self.bob).json(), [])

    def test_legacy_import_is_atomic_scoped_and_paused(self):
        from backend.platform.migrate_legacy import import_legacy
        ac = self.connection(self.alice)
        self.ingest(self.alice, ac)
        payload = {"format": 1, "controllers": [{"controller_id": "imported", "display_name": "Imported", "agent_id": "old-agent",
            "actuator_entity_ids": ["switch.keller"]}], "rules": [{"controller_id": "imported", "enabled": True, "version": 1,
            "schedule": {"type": "daily", "days": [], "at": "06:00", "tz": "Europe/Berlin"}, "action": {"type": "watering.start", "seconds": 10}, "conditions": []}]}
        with self.assertRaises(ValueError): import_legacy(self.app.state.db, payload, self.bh, ac["id"])
        self.assertEqual(import_legacy(self.app.state.db, payload, self.ah, ac["id"]), (1, 1))
        restored = self.app.state.db.one("SELECT payload FROM legacy_objects WHERE home_id=%s AND kind='rule'", (self.ah,))
        self.assertTrue(restored["payload"]["paused"])
        with self.assertRaises(ValueError): import_legacy(self.app.state.db, payload, self.ah, ac["id"])
        self.assertEqual(self.app.state.db.one("SELECT count(*) AS n FROM legacy_objects WHERE home_id=%s", (self.ah,))["n"], 2)

    def test_large_inventory_does_not_require_a_hardcoded_device_list(self):
        from backend.schemas.ha_messages import HAEntityState
        ac = self.connection(self.alice)
        self.app.state.db.ingest(self.ah, ac["id"], [HAEntityState(entity_id=f"switch.device_{n}", state="off") for n in range(5000)])
        page = self.client.get("/api/v1/devices?offset=4950&limit=50", headers=self.alice)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.json()["total"], 5000)
        self.assertEqual(len(page.json()["items"]), 50)
        targets = [d["id"] for d in self.app.state.db.all("SELECT id FROM devices WHERE home_id=%s LIMIT 500", (self.ah,))]
        spec = self.spec(targets[0], targets=targets)
        saved = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": spec})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(len(saved.json()["spec"]["targets"]), 500)

    def test_pending_rule_is_deployed_once_the_correct_agent_reconnects(self):
        ac = self.connection(self.alice)
        device = self.ingest(self.alice, ac)[0]
        rule = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": self.spec(device["id"])}).json()
        self.assertEqual(self.client.post("/api/v1/automations/" + rule["id"] + "/deploy", headers=self.alice, json={}).json()["status"], "pending")
        with self.client.websocket_connect("/api/v1/ws/agent/" + ac["id"], headers={"Authorization": "Bearer " + ac["token"]}) as ws:
            ws.send_json({"type": "metadata", "timezone": "Europe/Berlin"})
            ws.send_json({"type": "entities_snapshot", "entities": [{"entity_id": "switch.keller", "state": "off", "attributes": {}}]})
            command = ws.receive_json()
            self.assertEqual(command["name"], "automation.apply")
            ws.send_json({"type": "ack", "id": command["id"], "ok": True, "result": {"state": "on"}})
            for _ in range(30):
                current = self.client.get("/api/v1/automations", headers=self.alice).json()[0]
                if current["status"] == "active": break
                time.sleep(.01)
            self.assertEqual(current["status"], "active")
            self.assertEqual(current["deployed_version"], 1)

    def test_simulation_does_not_leave_an_armed_rule_for_a_later_real_connection(self):
        ac = self.connection(self.alice)
        device = self.ingest(self.alice, ac)[0]
        rule = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": self.spec(device["id"])}).json()
        with self.client.websocket_connect("/api/v1/ws/agent/" + ac["id"], headers={"Authorization": "Bearer " + ac["token"]}) as ws:
            ws.send_json({"type": "metadata", "timezone": "Europe/Berlin", "stub": True})
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json()["type"], "pong")
            result = self.client.post("/api/v1/automations/" + rule["id"] + "/deploy", headers=self.alice, json={}).json()
            self.assertEqual(result["status"], "error")
            self.assertFalse(result["requested"])

    def humidity_controller(self, headers=None):
        headers = headers or self.alice
        ac = self.connection(headers)
        self.ingest(headers, ac, 'switch.dehumidifier', attrs={'friendly_name': 'Entfeuchter', 'area_id': 'cellar', 'area_name': 'Keller'})
        items = self.ingest(headers, ac, 'sensor.humidity', '56', {'friendly_name': 'Raumfeuchte', 'device_class': 'humidity', 'unit_of_measurement': '%'})
        plug = next(d for d in items if d['entity_id'] == 'switch.dehumidifier')
        sensor = next(d for d in items if d['entity_id'] == 'sensor.humidity')
        zone = self.client.post('/api/v1/zones', headers=headers, json={'name': 'Benutzerzone'}).json()
        body = {'display_name': 'Keller entfeuchten', 'zone_id': zone['id'], 'targets': [plug['id']], 'sensor_id': sensor['id']}
        response = self.client.post('/api/v1/domains/humidity/controllers', headers=headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), body, ac

    def save_controller_rule(self, controller, **patch):
        spec = {'name': 'Trocken halten', 'kind': 'humidity_band', **controller['bindings'], **patch}
        return self.client.post('/api/v1/automations', headers=self.alice, json={'controller_id': controller['controller_id'], 'spec': spec})

    def test_controller_assigns_semantic_roles_and_owns_rules_in_user_zone(self):
        controller, body, _ = self.humidity_controller()
        roles = {e['entity_id']: e['role'] for e in controller['entities']}
        self.assertEqual(roles, {'switch.dehumidifier': 'dehumidifier_power', 'sensor.humidity': 'humidity_sensor'})
        self.assertEqual(controller['zone']['display_name'], 'Benutzerzone')
        response = self.save_controller_rule(controller)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['controller_id'], controller['controller_id'])
        listed = self.client.get('/api/v1/domains/humidity/zones/' + body['zone_id'] + '/controllers', headers=self.alice).json()
        self.assertEqual(listed[0]['rule_count'], 1)
        self.assertEqual(self.client.get('/api/v1/domains/humidity/zones/cellar/controllers', headers=self.alice).json(), [])
        # Rule inputs cannot independently redefine what the controller means.
        self.assertEqual(self.save_controller_rule(controller, kind='time_window', sensor_id=None, start='08:00', end='22:00').status_code, 422)

    def test_controller_zone_and_entity_access_are_household_scoped(self):
        controller, body, _ = self.humidity_controller()
        path = '/api/v1/domains/humidity/controllers/' + controller['controller_id']
        self.assertEqual(self.client.get(path, headers=self.bob).status_code, 404)
        self.assertEqual(self.client.put(path, headers=self.bob, json={**body, 'expected_version': 1}).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/automations', headers=self.bob, json={'controller_id': controller['controller_id'], 'spec': {'name': 'x', 'kind': 'humidity_band', **controller['bindings']}}).status_code, 404)
        grant = self.client.post('/api/v1/members', headers=self.alice, json={'user_id': self.client.get('/api/v1/auth/me', headers=self.bob).json()['id'], 'role': 'viewer'})
        self.assertEqual(grant.status_code, 200)
        viewer = {**self.bob, 'x-semantic-home-household': self.ah}
        self.assertEqual(self.client.post('/api/v1/zones', headers=viewer, json={'name': 'x'}).status_code, 403)
        self.assertEqual(self.client.put(path, headers=viewer, json={**body, 'expected_version': 1}).status_code, 403)

    def test_controller_rebinding_updates_drafts_and_rejects_stale_edits(self):
        controller, body, ac = self.humidity_controller()
        rule = self.save_controller_rule(controller).json()
        devices = self.ingest(self.alice, ac, 'switch.replacement')
        replacement = next(d for d in devices if d['entity_id'] == 'switch.replacement')
        path = '/api/v1/domains/humidity/controllers/' + controller['controller_id']
        changed = {**body, 'targets': [replacement['id']], 'expected_version': 1}
        response = self.client.put(path, headers=self.alice, json=changed)
        self.assertEqual(response.status_code, 200, response.text)
        updated = self.client.get('/api/v1/automations?controller_id=' + controller['controller_id'], headers=self.alice).json()[0]
        self.assertEqual(updated['id'], rule['id'])
        self.assertEqual(updated['spec']['targets'], [replacement['id']])
        self.assertEqual(updated['version'], 2)
        self.assertEqual(updated['status'], 'draft')
        self.assertEqual(self.client.put(path, headers=self.alice, json=changed).status_code, 409)
        self.assertEqual(self.client.put('/api/v1/automations/' + rule['id'], headers=self.alice, json={'spec': rule['spec'], 'expected_version': 1}).status_code, 422)

    def test_controller_cannot_rebind_or_delete_with_unconfirmed_local_rules(self):
        from psycopg.types.json import Jsonb
        controller, body, ac = self.humidity_controller()
        rule = self.save_controller_rule(controller).json()
        db = self.app.state.db
        devices = self.ingest(self.alice, ac, 'switch.replacement')
        target = next(d['id'] for d in devices if d['entity_id'] == 'switch.replacement')
        path = '/api/v1/domains/humidity/controllers/' + controller['controller_id']
        for deployed, requested, state in [(rule['spec'], True, 'active'), (None, True, 'pending')]:
            db.execute('UPDATE automations SET deployed_spec=%s,requested=%s,status=%s WHERE id=%s', (Jsonb(deployed) if deployed else None, requested, state, rule['id']))
            self.assertEqual(self.client.put(path, headers=self.alice, json={**body, 'targets': [target], 'expected_version': 1}).status_code, 409)
        self.assertEqual(self.client.delete(path, headers=self.alice).status_code, 409)
        # Renaming/moving the controller does not alter a running rule's inputs.
        rename = self.client.put(path, headers=self.alice, json={**body, 'display_name': 'Neuer Name', 'expected_version': 1})
        self.assertEqual(rename.status_code, 200, rename.text)

    def test_rule_lists_and_events_are_scoped_to_controller(self):
        first, _, _ = self.humidity_controller()
        second, _, _ = self.humidity_controller()
        rule = self.save_controller_rule(first).json()
        self.save_controller_rule(second)
        response = self.client.get('/api/v1/automations?controller_id=' + first['controller_id'], headers=self.alice).json()
        self.assertEqual([r['id'] for r in response], [rule['id']])
        events = self.client.get('/api/v1/automation-events?controller_id=' + first['controller_id'], headers=self.alice).json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['automation_id'], rule['id'])

    def test_controller_capabilities_validate_actual_roles(self):
        controller, body, ac = self.humidity_controller()
        items = self.ingest(self.alice, ac, 'sensor.temperature', '20', {'device_class': 'temperature', 'unit_of_measurement': '°C'})
        temperature = next(d['id'] for d in items if d['entity_id'] == 'sensor.temperature')
        self.assertEqual(self.client.post('/api/v1/domains/humidity/controllers', headers=self.alice, json={**body, 'sensor_id': temperature}).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/domains/humidity/controllers', headers=self.alice, json={**body, 'targets': body['targets'] * 2}).status_code, 422)
        self.assertEqual(self.client.post('/api/v1/domains/lighting/controllers', headers=self.alice, json={**body, 'sensor_id': None}).status_code, 200)
        self.assertEqual(self.client.post('/api/v1/domains/cleaning/controllers', headers=self.alice, json={**body, 'sensor_id': None}).status_code, 422)

    def test_controller_migration_preserves_rule_id_settings_and_intent(self):
        from pathlib import Path
        from psycopg.types.json import Jsonb
        db = self.app.state.db
        migrations = Path(__file__).parents[1] / 'src/backend/platform/migrations'
        with db.pool.connection() as conn, conn.transaction(force_rollback=True):
            conn.execute('CREATE SCHEMA test_controller_migration')
            conn.execute('SET LOCAL search_path TO test_controller_migration')
            conn.execute((migrations / '001_households.sql').read_text())
            conn.execute("INSERT INTO homes(id,name,timezone) VALUES ('h','Test','Europe/Berlin')")
            conn.execute("INSERT INTO agents(id,home_id,name,token_hash) VALUES ('a','h','Agent','hash')")
            conn.execute("INSERT INTO devices(id,home_id,agent_id,entity_id,state,attributes) VALUES ('plug','h','a','switch.plug','off',%s)", (Jsonb({'area_id': 'keller', 'area_name': 'Keller'}),))
            spec = {'name': 'Regel', 'kind': 'time_window', 'targets': ['plug'], 'sensor_id': None, 'enabled': True, 'start': '08:00', 'end': '22:00'}
            conn.execute("INSERT INTO automations(id,home_id,agent_id,spec,status,requested,version) VALUES ('rule','h','a',%s,'pending',true,7)", (Jsonb(spec),))
            conn.execute((migrations / '002_controller_zones.sql').read_text())
            rule = conn.execute('SELECT * FROM automations').fetchone()
            self.assertEqual((rule['id'], rule['version'], rule['status'], rule['requested'], rule['spec']), ('rule', 7, 'pending', True, spec))
            controller = conn.execute('SELECT * FROM semantic_controllers').fetchone()
            self.assertEqual((controller['id'], controller['zone_id']), (rule['controller_id'], 'keller'))

    def test_confirmed_paused_controller_rebinds_but_keeps_old_connection_cleanup(self):
        from psycopg.types.json import Jsonb
        controller, body, ac = self.humidity_controller()
        rule = self.save_controller_rule(controller).json()
        db = self.app.state.db
        db.execute("UPDATE automations SET spec=%s,deployed_spec=%s,deployed_version=1,status='paused',requested=true WHERE id=%s",
                   (Jsonb({**rule['spec'], 'enabled': False}), Jsonb({**rule['spec'], 'enabled': False}), rule['id']))
        devices = self.ingest(self.alice, ac, 'switch.replacement')
        replacement = next(d['id'] for d in devices if d['entity_id'] == 'switch.replacement')
        path = '/api/v1/domains/humidity/controllers/' + controller['controller_id']
        response = self.client.put(path, headers=self.alice, json={**body, 'targets': [replacement], 'expected_version': 1})
        self.assertEqual(response.status_code, 200, response.text)
        other_connection = self.connection(self.alice)
        devices = self.ingest(self.alice, other_connection, 'switch.other')
        other_plug = next(d['id'] for d in devices if d['entity_id'] == 'switch.other')
        devices = self.ingest(self.alice, other_connection, 'sensor.other', '50', {'device_class': 'humidity', 'unit_of_measurement': '%'})
        other_sensor = next(d['id'] for d in devices if d['entity_id'] == 'sensor.other')
        response = self.client.put(path, headers=self.alice, json={**body, 'targets': [other_plug], 'sensor_id': other_sensor, 'expected_version': 2})
        self.assertEqual(response.status_code, 409)

    def test_never_deployed_controller_draft_can_be_removed_offline(self):
        controller, _, _ = self.humidity_controller()
        rule = self.save_controller_rule(controller).json()
        response = self.client.delete('/api/v1/automations/' + rule['id'], headers=self.alice)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'deleted')
        self.assertFalse(response.json()['requested'])
        response = self.client.delete('/api/v1/domains/humidity/controllers/' + controller['controller_id'], headers=self.alice)
        self.assertEqual(response.status_code, 200, response.text)
