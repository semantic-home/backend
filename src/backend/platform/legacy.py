"""Household-scoped adapters for the existing watering API and runtime."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime

from fastapi import HTTPException
from pydantic_core import to_jsonable_python
from psycopg.types.json import Jsonb

from backend.controller_store.watering import WateringController, WateringControllerStore
from backend.demo_sessions import DemoSessionState
from backend.entities_store.entities import EntityRecord, EntityStore
from backend.rules_store.watering import Action, Condition, Rule, RulesStore, Schedule
from backend.scheduler.watering import ScheduledRuleState, WateringRuleScheduler
from backend.schemas.zone import Zone
from .security import home_context


class PersistentEntities(EntityStore):
    def __init__(self, db, home_id):
        super().__init__()
        self.db, self.home_id = db, home_id

    def list_all(self):
        rows = self.db.all("SELECT d.* FROM devices d JOIN agents a ON a.id=d.agent_id WHERE d.home_id=%s AND NOT a.revoked", (self.home_id,))
        # Legacy controllers use external IDs. Ambiguous IDs must never select an arbitrary connection.
        counts = {}
        for row in rows:
            counts[row["entity_id"]] = counts.get(row["entity_id"], 0) + 1
        return [EntityRecord(r["entity_id"], r["state"] if r["available"] else "unavailable", r["attributes"], "agent", r["agent_id"])
                for r in rows if counts[r["entity_id"]] == 1]

    def get(self, entity_id):
        rows = self.db.all("SELECT d.* FROM devices d JOIN agents a ON a.id=d.agent_id WHERE d.home_id=%s AND d.entity_id=%s AND NOT a.revoked",
                           (self.home_id, entity_id))
        if len(rows) != 1:
            return None
        r = rows[0]
        return EntityRecord(r["entity_id"], r["state"] if r["available"] else "unavailable", r["attributes"], "agent", r["agent_id"])

    def list_by_source(self, source):
        return self.list_all() if source == "agent" else []

    def upsert(self, entity_id, state, attributes, *, source="agent", source_id=None):
        if source_id and self.db.agent(self.home_id, source_id):
            self.db.execute("UPDATE devices SET state=%s,attributes=%s,seen_at=now() WHERE home_id=%s AND agent_id=%s AND entity_id=%s",
                            (state, Jsonb(attributes), self.home_id, source_id, entity_id))


def _dump(value):
    return to_jsonable_python(asdict(value))


class PersistentControllers(WateringControllerStore):
    def __init__(self, db, home_id):
        super().__init__()
        self.db, self.home_id = db, home_id
        for row in db.all("SELECT payload FROM legacy_objects WHERE home_id=%s AND kind='controller'", (home_id,)):
            data = row["payload"]
            if data.get("zone"):
                data["zone"] = Zone.model_validate(data["zone"])
            super().upsert(WateringController(**data))

    def upsert(self, controller):
        self.db.execute("INSERT INTO legacy_objects VALUES (%s,'controller',%s,%s) ON CONFLICT(home_id,kind,id) DO UPDATE SET payload=excluded.payload",
                        (self.home_id, controller.controller_id, Jsonb(_dump(controller))))
        super().upsert(controller)

    def delete(self, controller_id):
        self.db.execute("DELETE FROM legacy_objects WHERE home_id=%s AND kind='controller' AND id=%s", (self.home_id, controller_id))
        super().delete(controller_id)


class PersistentRules(RulesStore):
    def __init__(self, db, home_id):
        super().__init__()
        self.db, self.home_id = db, home_id
        for row in db.all("SELECT payload FROM legacy_objects WHERE home_id=%s AND kind='rule'", (home_id,)):
            data = row["payload"]
            data["schedule"] = Schedule.model_validate(data["schedule"])
            data["action"] = Action.model_validate(data["action"])
            data["conditions"] = [Condition.model_validate(c) for c in data["conditions"]]
            for key in ("created_at", "updated_at"):
                if data.get(key):
                    data[key] = datetime.fromisoformat(data[key])
            rule = Rule(**data)
            self._rules[rule.controller_id] = rule

    def _save(self, rule):
        self.db.execute("INSERT INTO legacy_objects VALUES (%s,'rule',%s,%s) ON CONFLICT(home_id,kind,id) DO UPDATE SET payload=excluded.payload",
                        (self.home_id, rule.controller_id, Jsonb(_dump(rule))))
        return rule

    def create(self, rule):
        stored = super().create(rule)
        try:
            return self._save(stored)
        except Exception:
            self._rules.pop(rule.controller_id, None)
            raise

    def update(self, rule, *, expected_version=None):
        previous = self.get(rule.controller_id)
        stored = super().update(rule, expected_version=expected_version)
        try:
            return self._save(stored)
        except Exception:
            self._rules[rule.controller_id] = previous
            raise

    def delete(self, controller_id):
        self.db.execute("DELETE FROM legacy_objects WHERE home_id=%s AND kind='rule' AND id=%s", (self.home_id, controller_id))
        super().delete(controller_id)


class PersistentScheduler(WateringRuleScheduler):
    def __init__(self, db, home_id, **kwargs):
        super().__init__(**kwargs)
        self.db, self.home_id = db, home_id
        self._saved = {}
        for row in db.all("SELECT id,payload FROM legacy_objects WHERE home_id=%s AND kind='schedule_state'", (home_id,)):
            payload = row["payload"]
            for key in ("next_run_at", "last_evaluated_at"):
                if payload.get(key):
                    payload[key] = datetime.fromisoformat(payload[key])
            self._states[row["id"]] = ScheduledRuleState(**payload)

    def _persist(self):
        for controller_id in set(self._saved) - set(self._states):
            self.db.execute("DELETE FROM legacy_objects WHERE home_id=%s AND kind='schedule_state' AND id=%s", (self.home_id, controller_id))
            self._saved.pop(controller_id, None)
        for controller_id, state in self._states.items():
            payload = _dump(state)
            if self._saved.get(controller_id) != payload:
                self.db.execute("INSERT INTO legacy_objects VALUES (%s,'schedule_state',%s,%s) ON CONFLICT(home_id,kind,id) DO UPDATE SET payload=excluded.payload",
                                (self.home_id, controller_id, Jsonb(payload)))
                self._saved[controller_id] = payload

    async def tick_once(self, **kwargs):
        await super().tick_once(**kwargs)
        self._persist()

    def set_paused(self, *args, **kwargs):
        result = super().set_paused(*args, **kwargs)
        self._persist()
        return result

    def skip_next(self, *args, **kwargs):
        result = super().skip_next(*args, **kwargs)
        self._persist()
        return result

    def delete(self, controller_id):
        self.db.execute("DELETE FROM legacy_objects WHERE home_id=%s AND kind='schedule_state' AND id=%s", (self.home_id, controller_id))
        self._saved.pop(controller_id, None)
        super().delete(controller_id)


class HomeScopes:
    def __init__(self, db, hub):
        self.db, self.hub, self.scopes = db, hub, {}

    def get(self, home_id):
        if not home_id:
            raise HTTPException(401, "An authenticated household is required")
        if home_id not in self.scopes:
            async def call(agent_id, domain, service, data):
                if not self.db.agent(home_id, agent_id):
                    raise RuntimeError("Agent does not belong to this household")
                entities = data.get("entity_id", [])
                entities = [entities] if isinstance(entities, str) else entities
                for eid in entities:
                    if not self.db.one("SELECT 1 FROM devices WHERE home_id=%s AND agent_id=%s AND entity_id=%s", (home_id, agent_id, eid)):
                        raise RuntimeError("Device does not belong to this connection")
                if domain == "valve":
                    service = {"turn_on": "open_valve", "turn_off": "close_valve"}.get(service, service)
                result = await self.hub.send_command_and_wait_ack(agent_id=agent_id, name="ha.call_service",
                    args={"domain": domain, "service": service, "data": data}, timeout_s=10)
                if not result.ok:
                    raise RuntimeError(result.error or "Local command failed")
            scope = DemoSessionState(home_id, call)
            scope.entity_store = PersistentEntities(self.db, home_id)
            scope.watering_controllers = PersistentControllers(self.db, home_id)
            scope.rules_store = PersistentRules(self.db, home_id)
            scope.watering_scheduler._task.cancel()
            scope.watering_scheduler = PersistentScheduler(self.db, home_id, entity_store=scope.entity_store,
                controller_store=scope.watering_controllers, rules_store=scope.rules_store, runtime=scope.watering_runtime)
            scope.watering_scheduler.start()
            self.scopes[home_id] = scope
        return self.scopes[home_id]

    async def shutdown(self):
        await asyncio.gather(*(s.shutdown() for s in self.scopes.values()))
        self.scopes.clear()


class ScopedProxy:
    def __init__(self, attribute):
        self.attribute = attribute

    def __getattr__(self, name):
        from backend.__main__ import home_scopes
        return getattr(getattr(home_scopes.get(home_context.get()), self.attribute), name)
