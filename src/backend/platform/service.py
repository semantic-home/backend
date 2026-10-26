from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from backend.semantic.capabilities import infer_capabilities
from .compiler import compile_automation
from .models import AutomationSpec
from .solar import solar_preview


class AutomationService:
    def __init__(self, db, hub):
        self.db, self.hub = db, hub
        self._locks: dict[str, asyncio.Lock] = {}

    def bindings(self, home_id, spec: AutomationSpec):
        records = self.db.all("SELECT d.* FROM devices d JOIN agents a ON a.id=d.agent_id WHERE d.home_id=%s AND d.id=ANY(%s) AND NOT a.revoked", (home_id, spec.targets))
        by_id = {record["id"]: record for record in records}
        targets = [by_id.get(target) for target in spec.targets]
        if any(d is None for d in targets):
            raise HTTPException(422, "A selected device does not belong to this household")
        sensors = [self.db.device(home_id, sensor_id) for sensor_id in spec.sensor_ids]
        if any(sensor is None for sensor in sensors):
            raise HTTPException(422, "A selected sensor does not belong to this household")
        if spec.kind == "humidity_band" and (not sensors or any(sensor["attributes"].get("device_class") != "humidity"
                                             or sensor["attributes"].get("unit_of_measurement") != "%" for sensor in sensors)):
            raise HTTPException(422, "Select room humidity sensors measured in %")
        agents = {d["agent_id"] for d in targets + sensors}
        if len(agents) != 1:
            raise HTTPException(422, "One local rule must use devices from the same Home Assistant connection")
        for d in targets:
            caps = infer_capabilities(d["entity_id"], d["attributes"])
            actions = caps.get("actions", [])
            if spec.kind == "cleaning":
                if d["entity_id"].split(".")[0] != "vacuum" or "start" not in actions:
                    raise HTTPException(422, "This device cannot start cleaning")
            elif not {"turn_on", "turn_off"}.issubset(actions):
                raise HTTPException(422, "This device cannot be switched on and off")
            if spec.brightness_pct is not None and "set_brightness" not in actions:
                raise HTTPException(422, "Every selected target must support brightness")
        return targets, sensors, next(iter(agents))

    async def deploy(self, home_id, rule_id):
        async with self._locks.setdefault(rule_id, asyncio.Lock()):
            rule = self.db.rule(home_id, rule_id)
            if not rule or not rule["requested"]:
                return rule
            spec = AutomationSpec.model_validate(rule["spec"])
            command_sent = False
            try:
                if not self.db.agent(home_id, rule["agent_id"]):
                    raise ValueError("Connection revoked. Local rules may still run; check Home Assistant or reconnect with new credentials.")
                if not self.hub.is_connected(rule["agent_id"]):
                    raise HTTPException(404, "Local agent is offline; waiting for reconnection")
                if rule["deleted"]:
                    args = {"config_id": "semantic_home_" + rule_id.replace("-", ""), "delete": True}
                else:
                    targets, sensors, agent_id = self.bindings(home_id, spec)
                    if agent_id != rule["agent_id"]:
                        raise ValueError("The automation connection has changed")
                    agent = self.db.agent(home_id, agent_id)
                    if agent["metadata"].get("stub"):
                        raise ValueError("Simulation mode cannot deploy real automations")
                    home = self.db.one("SELECT timezone FROM homes WHERE id=%s", (home_id,))
                    if spec.enabled and agent["metadata"].get("timezone") != home["timezone"]:
                        raise ValueError("Home Assistant and household timezones must match before deployment")
                    if spec.enabled and any(not d["available"] or d["state"] in {"unknown", "unavailable"} for d in targets + sensors):
                        raise ValueError("Selected devices are unavailable; reconnect and retry")
                    self.check_conflicts(home_id, rule_id, spec)
                    config = compile_automation(rule_id, spec, targets, sensors, rule["version"])
                    args = {"config_id": config["id"], "config": config, "timezone": home["timezone"]}
                self.db.execute("UPDATE automations SET status='pending',error=NULL WHERE home_id=%s AND id=%s AND version=%s",
                                (home_id, rule_id, rule["version"]))
                command_sent = True
                ack = await self.hub.send_command_and_wait_ack(agent_id=rule["agent_id"], name="automation.apply", args=args, timeout_s=20)
                if not ack.ok:
                    raise ValueError(ack.error or "The local agent did not confirm deployment")
                status = "deleted" if rule["deleted"] else ("active" if spec.enabled else "paused")
                self.db.execute("UPDATE automations SET status=%s,deployed_version=%s,deployed_spec=%s,error=NULL WHERE home_id=%s AND id=%s AND version=%s",
                                (status, rule["version"], Jsonb(spec.model_dump()), home_id, rule_id, rule["version"]))
                self.db.event(home_id, rule_id, status, {"version": rule["version"], "confirmation": ack.result})
            except (ValueError, RuntimeError, HTTPException) as exc:
                detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                offline = isinstance(exc, HTTPException) and exc.status_code == 404
                self.db.execute("UPDATE automations SET status=%s,error=%s,requested=%s WHERE home_id=%s AND id=%s AND version=%s",
                                ("pending" if offline else "error", detail, bool(offline or command_sent), home_id, rule_id, rule["version"]))
                self.db.event(home_id, rule_id, "deployment_failed", {"message": detail, "version": rule["version"]})
            return self.db.rule(home_id, rule_id)

    def check_conflicts(self, home_id, rule_id, spec):
        if not spec.enabled:
            return
        rows = self.db.all("SELECT id,spec,deployed_spec,requested FROM automations WHERE home_id=%s AND id<>%s AND "
                           "(deployed_version IS NOT NULL OR requested) AND status<>'deleted'", (home_id, rule_id))
        for row in rows:
            for other in [row["deployed_spec"], row["spec"] if row["requested"] else None]:
                if other and other["enabled"] and set(spec.targets).intersection(other["targets"]):
                    raise HTTPException(409, f"Pause the overlapping automation first: {other['name']}")
        # Preserve ownership of actuators already assigned to the watering runtime.
        target_ids = {d["entity_id"] for d in self.db.all("SELECT entity_id FROM devices WHERE home_id=%s AND id=ANY(%s)", (home_id, spec.targets))}
        for row in self.db.all("SELECT payload FROM legacy_objects WHERE home_id=%s AND kind='controller'", (home_id,)):
            ids = set(row["payload"].get("actuator_entity_ids", []))
            if target_ids.intersection(ids):
                raise HTTPException(409, "A selected actuator belongs to a watering controller")

    async def sync_agent(self, home_id, agent_id):
        for row in self.db.all("SELECT id FROM automations WHERE home_id=%s AND agent_id=%s AND requested ORDER BY updated_at", (home_id, agent_id)):
            await self.deploy(home_id, row["id"])

    def preview(self, home, spec):
        from .compiler import humidity_decision, window_active
        targets, sensors, agent_id = self.bindings(home["id"], spec)
        now = datetime.now(timezone.utc)
        value = None
        readings = []
        for sensor in sensors:
            try:
                if not sensor["available"]:
                    break
                raw_at = sensor["attributes"].get("_ha_last_updated")
                updated_at = datetime.fromisoformat(raw_at) if raw_at else sensor["seen_at"]
                candidate = float(sensor["state"])
                if not (0 <= candidate <= 100 and 0 <= (now - updated_at).total_seconds() < spec.sensor_stale_seconds):
                    break
                readings.append(candidate)
            except (ValueError, TypeError):
                break
        if sensors and len(readings) == len(sensors):
            value = max(readings)
        active = None if spec.uses_sun else window_active(spec, now, home["timezone"])
        schedule = None
        if spec.uses_sun:
            agent = self.db.agent(home["id"], agent_id)
            schedule = solar_preview(spec, agent["metadata"], agent["name"], home["timezone"], now, self.hub.is_connected(agent_id))
        items = []
        for target in targets:
            desired, reason = (humidity_decision(spec, value, target["state"] == "on", active)
                               if spec.kind == "humidity_band" else (active, "time_window"))
            items.append({"id": target["id"], "name": target["attributes"].get("friendly_name", target["entity_id"]),
                          "current": target["state"], "desired": ("scheduled" if spec.kind == "cleaning" or spec.uses_sun else ("on" if desired else "off")),
                          "reason": "solar_schedule" if spec.uses_sun else reason})
        return {"items": items, "humidity": value, "sensor_count": len(sensors), "humidity_aggregation": "maximum", "window_active": active, "timezone": home["timezone"],
                "agent_id": agent_id, "schedule": schedule,
                "note": "Preview sends no commands. Local availability and minimum switching intervals also apply."}
