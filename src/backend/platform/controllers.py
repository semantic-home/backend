"""Zone membership and entity meaning live on controllers, not on schedules."""
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .database import uid
from .models import AutomationSpec, SensorSelection
from .measurements import measurement_limits, measurement_status
from .program_models import AutomationProgram, input_ids
from backend.semantic.capabilities import infer_capabilities

KINDS = {"humidity": "humidity_band", "lighting": "time_window", "power": "time_window", "cleaning": "cleaning"}


def ensure_zones(db, home_id):
    # Import each HA area once; later HA renames do not overwrite user choices.
    db.execute("INSERT INTO semantic_zones(home_id,id,name,source) SELECT DISTINCT ON (d.attributes->>'area_id') "
               "d.home_id,d.attributes->>'area_id',coalesce(d.attributes->>'area_name',d.attributes->>'area_id'),'home_assistant' "
               "FROM devices d JOIN agents a ON a.id=d.agent_id WHERE d.home_id=%s AND NOT a.revoked "
               "AND d.attributes->>'area_id' IS NOT NULL ORDER BY d.attributes->>'area_id',d.seen_at DESC ON CONFLICT DO NOTHING", (home_id,))
    db.execute("INSERT INTO semantic_zones(home_id,id,name,source) SELECT DISTINCT ON (payload->'zone'->>'zone_id') "
               "home_id,payload->'zone'->>'zone_id',coalesce(payload->'zone'->>'display_name',payload->'zone'->>'zone_id'),'controller' "
               "FROM legacy_objects WHERE home_id=%s AND kind='controller' AND payload->'zone'->>'zone_id' IS NOT NULL ON CONFLICT DO NOTHING", (home_id,))
    db.execute("INSERT INTO semantic_zones(home_id,id,name) VALUES (%s,'unassigned','Nicht zugeordnet') ON CONFLICT DO NOTHING", (home_id,))


def get_controller(db, home_id, controller_id):
    row = db.one("SELECT c.*,z.name AS zone_name FROM semantic_controllers c JOIN semantic_zones z ON z.home_id=c.home_id AND z.id=c.zone_id "
                 "WHERE c.home_id=%s AND c.id=%s", (home_id, controller_id))
    if not row:
        raise HTTPException(404, "Unknown controller")
    return row


def controller_spec(controller, name=None):
    return AutomationSpec(name=name or controller["display_name"], kind=KINDS[controller["domain"]],
                          **controller["bindings"], **({"start": "08:00", "end": "22:00"} if controller["domain"] in {"lighting", "power"} else {}))


def binding_fields(spec):
    return {"targets": spec.targets, "sensor_id": spec.sensor_id, "sensor_ids": spec.sensor_ids}


def normalized_bindings(controller):
    original = controller["bindings"]
    selected = SensorSelection.model_validate({key: original[key] for key in ("sensor_id", "sensor_ids") if key in original})
    return {"targets": original["targets"], **selected.model_dump()}


def check_rule_binding(controller, spec):
    bindings = normalized_bindings(controller)
    allowed = {KINDS[controller["domain"]], 'program'} | ({"lighting_ramp"} if controller["domain"] == "lighting" else set())
    if (spec.kind not in allowed or spec.targets != bindings["targets"]
            or set(spec.sensor_ids) != set(bindings["sensor_ids"])):
        raise HTTPException(422, "Rule entities and purpose must match the controller. Edit the controller to change entity roles.")


def create_from_spec(db, home_id, spec, agent_id):
    """Compatibility for old API clients; a saved rule always has a controller."""
    domain = {"humidity_band": "humidity", "cleaning": "cleaning", "lighting_ramp": "lighting"}.get(spec.kind, "power")
    if spec.kind == "time_window" and db.one("SELECT id FROM devices WHERE home_id=%s AND id=ANY(%s) AND split_part(entity_id,'.',1)='light'", (home_id, spec.targets)):
        domain = "lighting"
    ensure_zones(db, home_id)
    target = db.device(home_id, spec.targets[0])
    zone_id = target["attributes"].get("area_id") or "unassigned"
    controller_id = uid()
    db.execute("INSERT INTO semantic_controllers(id,home_id,display_name,domain,zone_id,agent_id,bindings) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               (controller_id, home_id, spec.name, domain, zone_id, agent_id, Jsonb(binding_fields(spec))))
    return controller_id


def view(db, controller):
    bindings = normalized_bindings(controller)
    rules = db.all("SELECT id,spec,status,error,requested,deployed_spec FROM automations WHERE home_id=%s AND controller_id=%s AND status<>'deleted' ORDER BY updated_at DESC", (controller["home_id"], controller["id"]))
    ids = bindings["targets"] + bindings["sensor_ids"]
    limits={}
    for rule in rules:
        current=rule.get('deployed_spec') or rule['spec']
        if current.get('program'):
            ids+=input_ids(AutomationProgram.model_validate(current['program']))
        if current.get('enabled') and rule['status'] in {'active','pending','error'}:
            for key,seconds in measurement_limits(current).items():
                limits[key]=min(limits.get(key,86400),seconds)
    ids=list(dict.fromkeys(ids))
    devices = {d["id"]: d for d in db.all("SELECT d.*,a.revoked FROM devices d JOIN agents a ON a.id=d.agent_id WHERE d.home_id=%s AND d.id=ANY(%s)", (controller["home_id"], ids))}
    roles = []
    for device_id in ids:
        d = devices.get(device_id)
        if d:
            role = ("humidity_sensor" if device_id in bindings["sensor_ids"] else 'rule_input' if device_id not in bindings['targets'] else
                    {"humidity": "dehumidifier_power", "lighting": "lighting_output", "power": "power_output", "cleaning": "cleaner"}[controller["domain"]])
            roles.append({"device_id": device_id, "entity_id": d["entity_id"], "name": d["attributes"].get("friendly_name") or d["entity_id"],
                          "role": role, "state": d["state"], "unit": d["attributes"].get("unit_of_measurement"), "available": d["available"] and not d["revoked"],
                          'measurement':measurement_status({**d,'available':d['available'] and not d['revoked']},limits[device_id]) if device_id in limits else None})
            roles[-1]['capabilities']=infer_capabilities(d['entity_id'],d['attributes'])
    return {**controller, "bindings": bindings, "controller_id": controller["id"], "zone": {"zone_id": controller["zone_id"], "display_name": controller["zone_name"]},
            "entities": roles, "rules": rules, "rule_count": len(rules)}
