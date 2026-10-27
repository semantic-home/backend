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

    def test_sensor_selection_preserves_legacy_inputs_and_rejects_ambiguity(self):
        self.assertEqual(self.rule().sensor_ids, ['humidity'])
        multiple = AutomationSpec(name='Rooms', kind='humidity_band', targets=['plug'], sensor_ids=['kitchen', 'living'])
        self.assertEqual(multiple.sensor_ids, ['kitchen', 'living'])
        self.assertIsNone(multiple.sensor_id)
        self.assertEqual(AutomationSpec.model_validate(multiple.model_dump()).sensor_ids, multiple.sensor_ids)
        for patch in [{'sensor_ids': []}, {'sensor_ids': ['same', 'same']}, {'sensor_ids': ['']},
                      {'sensor_id': 'kitchen', 'sensor_ids': ['living']}, {'sensor_ids': ['kitchen'], 'kind': 'time_window', 'start': '08:00', 'end': '22:00'}]:
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                AutomationSpec.model_validate({'name': 'Rooms', 'kind': 'humidity_band', 'targets': ['plug'], **patch})

    def test_multiple_sensors_use_highest_reading_and_all_must_be_valid(self):
        from jinja2.nativetypes import NativeEnvironment
        from datetime import timedelta
        import math
        rule = AutomationSpec(name='Rooms', kind='humidity_band', targets=['plug'], sensor_ids=['kitchen', 'living'])
        sensors = [{'entity_id': 'sensor.' + room, 'attributes': {'registry_id': room, 'device_id': room}} for room in ['kitchen', 'living']]
        config = compile_automation('01234567-0123-4123-8123-0123456789ab', rule,
                                  [{'entity_id': 'switch.plug', 'attributes': {}}], sensors, 1)
        measurement_trigger = next(t for t in config['triggers'] if t['trigger'] == 'state' and 'to' not in t)
        self.assertEqual(measurement_trigger['entity_id'], ['sensor.kitchen', 'sensor.living'])
        self.assertEqual(set(config['variables']['semantic_home_bindings']), {'switch.plug', 'sensor.kitchen', 'sensor.living'})
        now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
        env = NativeEnvironment()
        def numeric(value):
            try: return math.isfinite(float(value))
            except ValueError: return False
        for kitchen, living, age, elapsed, identity, expected in [
            ('56', '51', 0, 200, True, [False, True]), ('51', '56', 0, 200, True, [False, True]),
            ('52', '51', 0, 200, True, [True, False]), ('51', '53', 0, 200, True, [False, False]),
            ('53.4', '51.5', 0, 200, True, [False, False]), ('55', '50', 0, 1, True, [False, False]),
            ('55', 'unknown', 0, 1, True, [True, False]), ('55', 'unavailable', 0, 1, True, [True, False]),
            ('55', 'nan', 0, 1, True, [True, False]), ('55', '101', 0, 1, True, [True, False]),
            ('55', '51', 1800, 1, True, [True, False]), ('55', '51', 0, 1, False, [True, False]),
        ]:
            class States:
                def __init__(self):
                    self.values = {'sensor.kitchen': SimpleNamespace(state=kitchen, last_updated=now),
                                   'sensor.living': SimpleNamespace(state=living, last_updated=now-timedelta(seconds=age)),
                                   'switch.plug': SimpleNamespace(last_changed=now-timedelta(seconds=elapsed))}
                def __call__(self, eid): return self.values[eid].state
                def __getitem__(self, eid): return self.values[eid]
            context = {'states': States(), 'now': lambda: now, 'as_timestamp': lambda value: value.timestamp(),
                       'is_number': numeric, 'device_id': lambda eid: eid.split('.')[1] if identity else 'replacement'}
            actual = [env.from_string(branch['conditions'][1]['value_template']).render(**context) for branch in config['actions'][0]['choose']]
            self.assertEqual(actual, expected, (kitchen, living, age, elapsed, identity))


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
        # Entity IDs can repeat across a household's HA connections. Return the
        # inventory for the connection populated above, not a household-wide mix.
        response = self.client.get("/api/v1/devices", headers=headers, params={"agent_id": connection["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def spec(self, device_id, **patch):
        return {"name": "Abendlicht", "kind": "time_window", "targets": [device_id], "start": "18:00", "end": "23:00", **patch}

    def test_reusable_program_roundtrips_and_rejects_foreign_or_cross_connection_inputs(self):
        ac,bc=self.connection(self.alice),self.connection(self.bob)
        target=self.ingest(self.alice,ac,'light.program','off',{'supported_color_modes':['brightness']})[0]
        own=self.ingest(self.alice,ac,'binary_sensor.motion','off',{'friendly_name':'Own motion'})
        own=next(d for d in own if d['entity_id']=='binary_sensor.motion')
        foreign=self.ingest(self.bob,bc,'binary_sensor.motion','off')[0]
        second=self.connection(self.alice)
        other=self.ingest(self.alice,second,'binary_sensor.motion','off')[0]
        body={'name':'Motion and time','kind':'program','targets':[target['id']],'program':{'version':2,
              'timing':{'mode':'events','triggers':[{'kind':'state','key':'motion','device_id':own['id'],'state':'on'}]},
              'conditions':{'kind':'all','terms':[{'kind':'clock','start':'18:00','end':'23:00'}]},
              'control':{'kind':'sequence','steps':[{'kind':'set','brightness_pct':30},{'kind':'wait','seconds':20},{'kind':'set','action':'turn_off'}]}}}
        response=self.client.post('/api/v1/automations',headers=self.alice,json={'spec':body})
        self.assertEqual(response.status_code,200,response.text)
        saved=response.json()
        self.assertFalse(saved['requested']);self.assertEqual(saved['spec']['program']['version'],2)
        preview=self.client.post('/api/v1/automations/preview',headers=self.alice,json=body)
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertIn('program',preview.json())
        for device in (foreign,other):
            changed={**body,'program':{**body['program'],'timing':{'mode':'events','triggers':[{'kind':'state','device_id':device['id'],'state':'on'}]}}}
            self.assertEqual(self.client.post('/api/v1/automations/preview',headers=self.alice,json=changed).status_code,422)
            self.assertEqual(self.client.post('/api/v1/automations',headers=self.alice,json={'spec':changed}).status_code,422)
        self.assertEqual(self.client.post('/api/v1/automations/preview',headers=self.bob,json=body).status_code,422)

    def test_unused_controller_inputs_are_validated_but_do_not_block_program_execution(self):
        from backend.platform.models import AutomationSpec
        controller,body,connection=self.humidity_controller()
        data={'name':'Timed plug','kind':'program','targets':body['targets'],'sensor_ids':[body['sensor_id']],
              'program':{'timing':{'mode':'always'},'control':{'kind':'switch'}}}
        targets,inputs,agent_id=self.app.state.automations.bindings(self.ah,AutomationSpec.model_validate(data))
        self.assertEqual(inputs,[])
        self.assertEqual(agent_id,connection['id'])
        foreign_connection=self.connection(self.bob)
        foreign=self.ingest(self.bob,foreign_connection,'sensor.foreign','55',{'device_class':'humidity','unit_of_measurement':'%'})[0]
        response=self.client.post('/api/v1/automations/preview',headers=self.alice,json={**data,'sensor_ids':[foreign['id']]})
        self.assertEqual(response.status_code,422,response.text)

    def test_generic_range_inputs_and_stale_status_are_visible_on_the_controller(self):
        from datetime import timedelta
        ac=self.connection(self.alice)
        self.ingest(self.alice,ac,'switch.range','off',{'friendly_name':'Range output'})
        self.ingest(self.alice,ac,'sensor.first','20',{'unit_of_measurement':'°C','device_class':'temperature','_ha_last_updated':datetime.now(timezone.utc).isoformat()})
        inventory=self.ingest(self.alice,ac,'sensor.second','22',{'unit_of_measurement':'°C','device_class':'temperature','_ha_last_updated':datetime.now(timezone.utc).isoformat()})
        by_entity={d['entity_id']:d for d in inventory}
        output=by_entity['switch.range'];first=by_entity['sensor.first'];second=by_entity['sensor.second']
        controller=self.client.post('/api/v1/domains/power/controllers',headers=self.alice,json={'display_name':'Heating','zone_id':'unassigned','targets':[output['id']]}).json()
        program={'version':2,'timing':{'mode':'always'},'end_action':'off','control':{'kind':'band','sensor_ids':[first['id'],second['id']],'aggregation':'average','demand':'below','lower':22,'upper':24}}
        body={'name':'Maintain temperature','kind':'program','targets':[output['id']],'program':program}
        response=self.client.post('/api/v1/automations',headers=self.alice,json={'spec':body,'controller_id':controller['controller_id']})
        self.assertEqual(response.status_code,200,response.text)
        saved=response.json()
        preview=self.client.post('/api/v1/automations/preview',headers=self.alice,json=body)
        self.assertEqual(preview.status_code,200,preview.text)
        self.assertEqual(preview.json()['program']['aggregate'],21)
        self.assertEqual(preview.json()['items'][0]['desired'],'on')
        self.app.state.db.execute("UPDATE automations SET deployed_spec=spec,deployed_version=version,status='active' WHERE id=%s",(saved['id'],))
        self.ingest(self.alice,ac,'sensor.second','22',{'unit_of_measurement':'°C','device_class':'temperature','_ha_last_updated':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()})
        view=self.client.get('/api/v1/domains/power/controllers/'+controller['controller_id'],headers=self.alice)
        self.assertEqual(view.status_code,200,view.text)
        measurements={e['device_id']:e['measurement'] for e in view.json()['entities'] if e['measurement']}
        self.assertFalse(measurements[second['id']]['valid']);self.assertEqual(measurements[second['id']]['issue'],'stale')
        self.assertTrue(measurements[first['id']]['valid'])
        preview=self.client.post('/api/v1/automations/preview',headers=self.alice,json=body)
        self.assertEqual(preview.json()['items'][0]['desired'],'off')
        self.assertEqual(preview.json()['items'][0]['reason'],'sensor_unavailable')

    def test_program_actions_cannot_target_unassigned_outputs(self):
        ac=self.connection(self.alice)
        first=self.ingest(self.alice,ac,'switch.first')[0]
        inventory=self.ingest(self.alice,ac,'switch.second')
        second=next(d for d in inventory if d['entity_id']=='switch.second')
        rule={'name':'Out-of-controller action','kind':'program','targets':[first['id']],
              'program':{'timing':{'mode':'events','triggers':[{'kind':'anchor','anchor':{'event':'clock','at':'08:00'}}]},'control':{'kind':'sequence','steps':[{'kind':'set','targets':[second['id']]}]}}}
        self.assertEqual(self.client.post('/api/v1/automations',headers=self.alice,json={'spec':rule}).status_code,422)
        self.assertEqual(self.client.post('/api/v1/automations/preview',headers=self.alice,json=rule).status_code,422)

    def test_solar_schedule_persists_and_previews_only_its_connection_location(self):
        from psycopg.types.json import Jsonb
        ac, bc = self.connection(self.alice), self.connection(self.bob)
        lamp = self.ingest(self.alice, ac, "light.room", "off", {"friendly_name": "Reading lamp", "supported_color_modes": ["brightness"]})[0]
        self.app.state.db.execute("UPDATE agents SET metadata=%s WHERE id=%s", (Jsonb({"timezone": "Europe/Berlin", "location": {
            "name": "Meckenheim", "latitude": 50.625, "longitude": 7.022, "elevation": 180}}), ac["id"]))
        self.app.state.db.execute("UPDATE agents SET metadata=%s WHERE id=%s", (Jsonb({"timezone": "Europe/Berlin", "location": {
            "name": "Other household", "latitude": 0, "longitude": 0}}), bc["id"]))
        spec = self.spec(lamp["id"], start=None, start_event="sunset", start_offset_minutes=-30)
        created = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": spec})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["status"], "draft")
        saved = self.client.get("/api/v1/automations", headers=self.alice).json()[0]
        self.assertEqual((saved["spec"]["start_event"], saved["spec"]["start_offset_minutes"], saved["spec"]["start"]), ("sunset", -30, None))
        response = self.client.post("/api/v1/automations/preview", headers=self.alice, json=saved["spec"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["schedule"]["location_name"], "Meckenheim")
        self.assertTrue(response.json()["schedule"]["events"])
        self.assertEqual(response.json()["items"][0]["desired"], "scheduled")
        self.assertIsNone(response.json()["window_active"])
        self.assertEqual(self.client.post("/api/v1/automations/preview", headers=self.bob, json=spec).status_code, 422)
        # Neither saving nor previewing queued an actual device command.
        self.assertFalse(self.app.state.db.rule(self.ah, saved["id"])["requested"])

    def test_agent_location_metadata_is_household_scoped_and_preview_is_backward_compatible(self):
        ac = self.connection(self.alice)
        lamp = self.ingest(self.alice, ac)[0]
        rule = self.spec(lamp["id"], start=None, start_event="sunrise")
        missing = self.client.post("/api/v1/automations/preview", headers=self.alice, json=rule)
        self.assertEqual(missing.status_code, 200, missing.text)
        self.assertFalse(missing.json()["schedule"]["available"])
        with self.client.websocket_connect("/api/v1/ws/agent/" + ac["id"], headers={"Authorization": "Bearer " + ac["token"]}) as ws:
            ws.send_json({"type": "metadata", "timezone": "Europe/Berlin", "location": {"name": "Home location", "latitude": 50, "longitude": 7}})
            ws.send_json({"type": "ping"})
            self.assertEqual(ws.receive_json()["type"], "pong")
            own = self.client.get("/api/v1/connections", headers=self.alice).json()[0]
            self.assertEqual(own["metadata"]["location"]["name"], "Home location")
            self.assertEqual(self.client.get("/api/v1/connections", headers=self.bob).json(), [])

    def test_dawn_group_rule_persists_on_lighting_controller_and_keeps_home_and_connection_boundaries(self):
        from psycopg.types.json import Jsonb
        from backend.platform.models import DawnRamp
        ac, bc = self.connection(self.alice), self.connection(self.bob)
        attrs = {"friendly_name": "Dawn lamp", "supported_color_modes": ["color_temp"],
                 "min_color_temp_kelvin": 2702, "max_color_temp_kelvin": 6666, "supported_features": 44}
        self.ingest(self.alice, ac, "light.first", attrs=attrs)
        items = self.ingest(self.alice, ac, "light.second", attrs=attrs)
        targets = [d["id"] for d in items]
        foreign = self.ingest(self.bob, bc, "light.first", attrs=attrs)[0]
        self.app.state.db.execute("UPDATE agents SET metadata=%s WHERE id=%s", (Jsonb({"timezone": "Europe/Berlin", "location": {
            "name": "Meckenheim", "latitude": 50.625, "longitude": 7.022}}), ac["id"]))
        zone = self.client.post("/api/v1/zones", headers=self.alice, json={"name": "Living room"}).json()
        response = self.client.post("/api/v1/domains/lighting/controllers", headers=self.alice, json={
            "display_name": "Dawn lamps", "zone_id": zone["id"], "targets": targets})
        self.assertEqual(response.status_code, 200, response.text)
        controller = response.json()
        rule = {"name": "Real dawn", "kind": "lighting_ramp", "targets": targets, "ramp": DawnRamp().model_dump()}
        response = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": rule, "controller_id": controller["controller_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        self.assertEqual((saved["status"], saved["spec"]["ramp"]["start_at"]), ("draft", "06:00"))
        self.assertFalse(saved["requested"])
        response = self.client.post("/api/v1/automations/preview", headers=self.alice, json=rule)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ramp"]["location_name"], "Meckenheim")
        self.assertEqual(len(response.json()["ramp"]["lamps"]), 2)
        self.assertTrue(all(d["desired"] == "scheduled" for d in response.json()["items"]))
        self.assertIsNone(response.json()["window_active"])
        # An evening companion persists independently on the same controller.
        dusk = {**rule, "name": "Real dusk", "ramp": {**rule["ramp"], "direction": "dusk", "start_at": "16:00",
                "stop_at": "23:00", "initial_brightness_pct": 100, "off_at_end": True}}
        response = self.client.post("/api/v1/automations", headers=self.alice, json={"spec": dusk, "controller_id": controller["controller_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["spec"]["ramp"]["direction"], "dusk")
        rules = self.client.get("/api/v1/automations?controller_id=" + controller["controller_id"], headers=self.alice).json()
        self.assertEqual({r["spec"]["ramp"]["direction"] for r in rules}, {"dawn", "dusk"})
        preview = self.client.post("/api/v1/automations/preview", headers=self.alice, json=dusk)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual([p["key"] for p in preview.json()["ramp"]["phases"]], ["daylight", "civil", "nautical", "astronomical"])
        # New rule kinds use exactly the same household and connection isolation.
        self.assertEqual(self.client.post("/api/v1/automations/preview", headers=self.alice, json={**rule, "targets": [targets[0], foreign["id"]]}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/automations/preview", headers=self.bob, json=rule).status_code, 422)
        other_connection = self.connection(self.alice)
        other = self.ingest(self.alice, other_connection, "light.third", attrs=attrs)[0]
        self.assertEqual(self.client.post("/api/v1/automations/preview", headers=self.alice, json={**rule, "targets": [targets[0], other["id"]]}).status_code, 422)
        plain = self.ingest(self.alice, ac, "light.plain", attrs={"supported_color_modes": ["brightness"]})
        plain_id = next(d["id"] for d in plain if d["entity_id"] == "light.plain")
        self.assertEqual(self.client.post("/api/v1/automations/preview", headers=self.alice, json={**rule, "targets": [plain_id]}).status_code, 422)
        brightness = {**rule, "targets": [plain_id], "ramp": {**rule["ramp"], "color_mode": "brightness"}}
        self.assertEqual(self.client.post("/api/v1/automations/preview", headers=self.alice, json=brightness).status_code, 200)
        power = self.client.post("/api/v1/domains/power/controllers", headers=self.alice, json={"display_name": "Power", "zone_id": zone["id"], "targets": targets}).json()
        self.assertEqual(self.client.post("/api/v1/automations", headers=self.alice, json={"spec": rule, "controller_id": power["controller_id"]}).status_code, 422)

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

    def test_multiple_humidity_sensors_persist_on_controller_and_rule_and_preview_maximum(self):
        controller, body, ac = self.humidity_controller()
        items = self.ingest(self.alice, ac, 'sensor.second_room', '51.5',
                            {'device_class': 'humidity', 'unit_of_measurement': '%', 'registry_id': 'second-room'})
        second = next(d['id'] for d in items if d['entity_id'] == 'sensor.second_room')
        sensors = [body['sensor_id'], second]
        path = '/api/v1/domains/humidity/controllers/' + controller['controller_id']
        response = self.client.put(path, headers=self.alice, json={**body, 'sensor_id': None, 'sensor_ids': sensors, 'expected_version': 1})
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()
        self.assertEqual(updated['bindings']['sensor_ids'], sensors)
        self.assertIsNone(updated['bindings']['sensor_id'])
        self.assertEqual(len([e for e in updated['entities'] if e['role'] == 'humidity_sensor']), 2)
        self.assertEqual(self.client.get(path, headers=self.alice).json()['bindings']['sensor_ids'], sensors)
        rule = self.save_controller_rule(updated)
        self.assertEqual(rule.status_code, 200, rule.text)
        self.assertEqual(rule.json()['spec']['sensor_ids'], sensors)
        preview = self.client.post('/api/v1/automations/preview', headers=self.alice, json=rule.json()['spec'])
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual((preview.json()['humidity'], preview.json()['sensor_count'], preview.json()['items'][0]['desired']), (56, 2, 'on'))
        # Missing, stale, invalid or unavailable readings cannot silently disappear
        # from the aggregate. Any one invalid sensor makes the output fail off.
        for state, attrs, available in [('unavailable', {}, True), ('nan', {}, True), ('51', {'_ha_last_updated': '2020-01-01T00:00:00+00:00'}, True), ('51', {}, False)]:
            self.ingest(self.alice, ac, 'sensor.second_room', state, {'device_class': 'humidity', 'unit_of_measurement': '%', **attrs})
            self.app.state.db.execute('UPDATE devices SET available=%s WHERE id=%s', (available, second))
            preview = self.client.post('/api/v1/automations/preview', headers=self.alice, json=rule.json()['spec']).json()
            self.assertIsNone(preview['humidity'])
            self.assertEqual(preview['items'][0]['desired'], 'off')
        # A rule cannot quietly omit one of the controller's selected sensors.
        self.assertEqual(self.save_controller_rule(updated, sensor_ids=[sensors[0]], sensor_id=sensors[0]).status_code, 422)

    def test_additional_humidity_sensor_cannot_cross_home_or_connection(self):
        controller, body, _ = self.humidity_controller()
        path = '/api/v1/domains/humidity/controllers/' + controller['controller_id']
        for headers, expected in [(self.alice, 'same Home Assistant connection'), (self.bob, 'does not belong to this household')]:
            other = self.connection(headers)
            items = self.ingest(headers, other, 'sensor.other', '55', {'device_class': 'humidity', 'unit_of_measurement': '%'})
            sensor = next(d['id'] for d in items if d['entity_id'] == 'sensor.other')
            response = self.client.put(path, headers=self.alice, json={**body, 'sensor_id': None, 'sensor_ids': [body['sensor_id'], sensor], 'expected_version': 1})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn(expected, response.json()['detail'])
        self.assertEqual(self.client.get(path, headers=self.alice).json()['bindings']['sensor_ids'], [body['sensor_id']])

    def test_legacy_single_sensor_controller_normalizes_without_changing_rule_intent(self):
        from psycopg.types.json import Jsonb
        controller, body, _ = self.humidity_controller()
        rule = self.save_controller_rule(controller).json()
        # Simulate the exact stored bindings used before multi-sensor support.
        self.app.state.db.execute('UPDATE semantic_controllers SET bindings=%s WHERE id=%s',
                                  (Jsonb({'targets': body['targets'], 'sensor_id': body['sensor_id']}), controller['controller_id']))
        path = '/api/v1/domains/humidity/controllers/' + controller['controller_id']
        response = self.client.put(path, headers=self.alice, json={**body, 'display_name': 'Renamed', 'expected_version': 1})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['bindings']['sensor_ids'], [body['sensor_id']])
        saved = self.app.state.db.rule(self.ah, rule['id'])
        self.assertEqual(saved['version'], rule['version'])
        self.assertEqual(saved['spec'], rule['spec'])

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

    def test_controller_fixtures_keep_duplicate_entity_ids_on_their_own_connection(self):
        from unittest.mock import patch

        # Give the two connections opposite inventory ordering for the plug and
        # sensor. A household-wide first-match lookup must not wire them together.
        device_ids = [f'00000000-0000-4000-8000-{number:012d}' for number in (1, 4, 3, 2)]
        with patch('backend.platform.database.uid', side_effect=device_ids):
            first, _, first_connection = self.humidity_controller()
            second, _, second_connection = self.humidity_controller()

        for controller, connection, expected_ids in (
            (first, first_connection, (device_ids[0], device_ids[1])),
            (second, second_connection, (device_ids[2], device_ids[3])),
        ):
            self.assertEqual(controller['agent_id'], connection['id'])
            self.assertEqual(controller['bindings']['targets'], [expected_ids[0]])
            self.assertEqual(controller['bindings']['sensor_id'], expected_ids[1])

        inventory = self.client.get('/api/v1/devices', headers=self.alice).json()
        self.assertEqual(inventory['total'], 4)
        self.assertEqual({device['agent_id'] for device in inventory['items']},
                         {first_connection['id'], second_connection['id']})

    def test_controller_rejects_mixed_connections_and_foreign_households(self):
        controller, body, _ = self.humidity_controller()
        path = '/api/v1/domains/humidity/controllers'

        for headers, expected_detail in (
            (self.alice, 'same Home Assistant connection'),
            (self.bob, 'does not belong to this household'),
        ):
            connection = self.connection(headers)
            self.ingest(headers, connection, 'switch.dehumidifier')
            inventory = self.ingest(headers, connection, 'sensor.humidity', '56',
                                    {'device_class': 'humidity', 'unit_of_measurement': '%'})
            plug = next(device['id'] for device in inventory if device['entity_id'] == 'switch.dehumidifier')
            sensor = next(device['id'] for device in inventory if device['entity_id'] == 'sensor.humidity')

            for patch in ({'targets': [plug]}, {'sensor_id': sensor}):
                with self.subTest(connection=connection['id'], changed_role=next(iter(patch))):
                    create = self.client.post(path, headers=self.alice, json={**body, **patch})
                    self.assertEqual(create.status_code, 422, create.text)
                    self.assertIn(expected_detail, create.json()['detail'])
                    update = self.client.put(path + '/' + controller['controller_id'], headers=self.alice,
                                             json={**body, **patch, 'expected_version': controller['version']})
                    self.assertEqual(update.status_code, 422, update.text)

        saved = self.client.get(path + '/' + controller['controller_id'], headers=self.alice).json()
        self.assertEqual(saved['bindings'], controller['bindings'])
        self.assertEqual(saved['version'], controller['version'])
        self.assertEqual(len(self.client.get(path, headers=self.alice).json()), 1)

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
