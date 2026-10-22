from __future__ import annotations

import asyncio
import hmac
import secrets

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import Field

from backend.semantic.capabilities import infer_capabilities
from backend.settings.config import settings
from .database import token_hash, uid
from .models import AutomationSpec, Credentials, DeviceAction, HomeCreate, SaveAutomation, StrictModel, ZoneCreate
from .controllers import check_rule_binding, create_from_spec, ensure_zones, get_controller
from .security import password_hash, password_matches

router = APIRouter()
_DUMMY_PASSWORD_HASH = password_hash("unavailable-account")


def _cookie(response, token):
    response.set_cookie("semantic_session", token, httponly=True, secure=settings.auth_cookie_secure,
                        samesite="strict", max_age=7 * 86400, path="/")


def _rate_limit(request, email):
    db = request.app.state.db
    source = request.client.host if request.client else "local"
    for key, maximum in (("account:" + token_hash(email), 30), ("source:" + source, 1000)):
        row = db.one("INSERT INTO auth_attempts VALUES (%s,1,now()+interval '15 minutes') ON CONFLICT(key) DO UPDATE SET "
                 "attempts=CASE WHEN auth_attempts.expires_at < now() THEN 1 ELSE auth_attempts.attempts+1 END,"
                 "expires_at=CASE WHEN auth_attempts.expires_at < now() THEN now()+interval '15 minutes' ELSE auth_attempts.expires_at END RETURNING attempts", (key,))
        if row["attempts"] > maximum:
            raise HTTPException(429, "Too many sign-in attempts. Please wait 15 minutes.")


@router.post("/auth/register")
def register(req: Credentials, request: Request, response: Response):
    _rate_limit(request, req.email)
    if settings.registration_key and not hmac.compare_digest(req.registration_key, settings.registration_key):
        raise HTTPException(403, "Invalid registration key")
    db, user_id = request.app.state.db, uid()
    try:
        db.execute("INSERT INTO users(id,email,password_hash) VALUES (%s,%s,%s)",
                   (user_id, req.email, password_hash(req.password)))
    except UniqueViolation:
        raise HTTPException(409, "An account with this email already exists")
    _cookie(response, db.session(user_id))
    return {"id": user_id, "email": req.email, "homes": []}


@router.post("/auth/login")
def login(req: Credentials, request: Request, response: Response):
    _rate_limit(request, req.email)
    db = request.app.state.db
    user = db.one("SELECT * FROM users WHERE email=%s", (req.email,))
    # Match the hashing cost for unknown accounts as well.
    encoded = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
    if not password_matches(req.password, encoded) or not user:
        raise HTTPException(401, "Invalid email or password")
    _cookie(response, db.session(user["id"]))
    return {"id": user["id"], "email": user["email"], "homes": db.homes(user["id"])}


@router.get("/auth/me")
def me(request: Request):
    db = request.app.state.db
    user = db.user(request.cookies.get("semantic_session", ""))
    if not user:
        raise HTTPException(401, "Please sign in")
    return {**user, "homes": db.homes(user["id"])}


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    request.app.state.db.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash(request.cookies.get("semantic_session", "")),))
    response.delete_cookie("semantic_session", path="/")
    return {"ok": True}


@router.get("/homes")
def homes(request: Request):
    return request.app.state.db.homes(request.state.user["id"])


@router.post("/homes")
def create_home(req: HomeCreate, request: Request):
    db, home_id = request.app.state.db, uid()
    with db.pool.connection() as conn:
        conn.execute("INSERT INTO homes(id,name,timezone) VALUES (%s,%s,%s)", (home_id, req.name, req.timezone))
        conn.execute("INSERT INTO memberships VALUES (%s,%s,'owner')", (home_id, request.state.user["id"]))
    return {"id": home_id, "name": req.name, "timezone": req.timezone, "role": "owner"}


class Member(StrictModel):
    user_id: str
    role: str = Field(pattern="^(editor|viewer)$")


@router.post("/members")
def add_member(req: Member, request: Request):
    if request.state.home["role"] != "owner":
        raise HTTPException(403, "Only the owner can add members")
    db = request.app.state.db
    # Email addresses are not verified in this release. Membership therefore uses
    # the account ID explicitly shared by the intended person, not an email claim.
    user = db.one("SELECT id FROM users WHERE id=%s", (req.user_id,))
    if not user:
        raise HTTPException(404, "The member must register first")
    existing = db.one("SELECT role FROM memberships WHERE home_id=%s AND user_id=%s", (request.state.home["id"], user["id"]))
    if existing and existing["role"] == "owner":
        raise HTTPException(409, "The owner role cannot be replaced")
    db.execute("INSERT INTO memberships VALUES (%s,%s,%s) ON CONFLICT(home_id,user_id) DO UPDATE SET role=excluded.role",
               (request.state.home["id"], user["id"], req.role))
    return {"ok": True}


@router.get("/members")
def list_members(request: Request):
    if request.state.home["role"] != "owner":
        raise HTTPException(403, "Only the owner can manage members")
    return request.app.state.db.all("SELECT u.id,u.email,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.home_id=%s ORDER BY u.email", (request.state.home["id"],))


@router.delete("/members/{user_id}")
def remove_member(user_id: str, request: Request):
    if request.state.home["role"] != "owner":
        raise HTTPException(403, "Only the owner can remove members")
    changed = request.app.state.db.execute("DELETE FROM memberships WHERE home_id=%s AND user_id=%s AND role<>'owner'", (request.state.home["id"], user_id))
    if not changed:
        raise HTTPException(404, "Unknown removable membership")
    return {"ok": True}


class AgentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)


@router.post("/connections")
def create_connection(req: AgentCreate, request: Request):
    db, agent_id, token = request.app.state.db, uid(), secrets.token_urlsafe(40)
    db.execute("INSERT INTO agents(id,home_id,name,token_hash) VALUES (%s,%s,%s,%s)",
               (agent_id, request.state.home["id"], req.name, token_hash(token)))
    return {"id": agent_id, "name": req.name, "token": token}


@router.get("/connections")
async def connections(request: Request):
    records = {r.agent_id: r for r in await request.app.state.agent_hub.list_agents()}
    rows = request.app.state.db.all("SELECT id,name,metadata,revoked FROM agents WHERE home_id=%s ORDER BY name", (request.state.home["id"],))
    return [{**row, "connected": bool(records.get(row["id"]) and records[row["id"]].connected)} for row in rows]


@router.delete("/connections/{agent_id}")
async def revoke_connection(agent_id: str, request: Request):
    db, home_id = request.app.state.db, request.state.home["id"]
    if not db.agent(home_id, agent_id):
        raise HTTPException(404, "Unknown connection")
    db.execute("UPDATE agents SET revoked=true WHERE id=%s AND home_id=%s", (agent_id, home_id))
    db.execute("UPDATE devices SET available=false WHERE home_id=%s AND agent_id=%s", (home_id, agent_id))
    db.execute("UPDATE automations SET status='error',requested=false,error=%s WHERE home_id=%s AND agent_id=%s AND status<>'deleted'",
               ("Connection revoked. Local rules may still run; check Home Assistant or reconnect with new credentials.", home_id, agent_id))
    await request.app.state.agent_hub.disconnect(agent_id)
    return {"ok": True, "local_rules_may_continue": True}


@router.post("/connections/{agent_id}/rotate")
async def rotate_connection(agent_id: str, request: Request):
    db, home_id = request.app.state.db, request.state.home["id"]
    row = db.one("SELECT name FROM agents WHERE id=%s AND home_id=%s", (agent_id, home_id))
    if not row:
        raise HTTPException(404, "Unknown connection")
    token = secrets.token_urlsafe(40)
    db.execute("UPDATE agents SET token_hash=%s,revoked=false WHERE home_id=%s AND id=%s", (token_hash(token), home_id, agent_id))
    await request.app.state.agent_hub.disconnect(agent_id)
    return {"id": agent_id, "name": row["name"], "token": token}


@router.get("/devices")
def devices(request: Request, q: str = Query(default="", max_length=100), area: str | None = None,
            agent_id: str | None = None,
            capability: str | None = Query(default=None, pattern="^(humidity|switching|cleaning)$"),
            offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200)):
    db, home_id = request.app.state.db, request.state.home["id"]
    where, params = "d.home_id=%s AND NOT a.revoked", [home_id]
    if q:
        where += " AND (d.entity_id ILIKE %s OR d.attributes->>'friendly_name' ILIKE %s)"
        params += ["%" + q + "%"] * 2
    if area:
        where += " AND d.attributes->>'area_id'=%s"
        params += [area]
    if agent_id:
        where += " AND d.agent_id=%s"
        params += [agent_id]
    if capability == "humidity":
        where += " AND d.attributes->>'device_class'='humidity' AND d.attributes->>'unit_of_measurement'=%s"
        params += ["%"]
    elif capability == "switching":
        where += " AND split_part(d.entity_id,'.',1) IN ('switch','light','humidifier')"
    elif capability == "cleaning":
        where += " AND split_part(d.entity_id,'.',1)='vacuum'"
    total = db.one("SELECT count(*) AS n FROM devices d JOIN agents a ON a.id=d.agent_id WHERE " + where, params)["n"]
    rows = db.all("SELECT d.*,a.name AS connection_name FROM devices d JOIN agents a ON a.id=d.agent_id WHERE " + where +
                  " ORDER BY lower(coalesce(d.attributes->>'friendly_name',d.entity_id)),d.id LIMIT %s OFFSET %s", [*params, limit, offset])
    for row in rows:
        row["capabilities"] = infer_capabilities(row["entity_id"], row["attributes"])
    areas = db.all("SELECT DISTINCT d.attributes->>'area_id' AS id,d.attributes->>'area_name' AS name FROM devices d "
                   "JOIN agents a ON a.id=d.agent_id WHERE d.home_id=%s AND NOT a.revoked AND d.attributes->>'area_id' IS NOT NULL", (home_id,))
    return {"items": rows, "total": total, "offset": offset, "limit": limit, "areas": areas}


@router.get("/rooms")
async def rooms(request: Request):
    from backend.__main__ import demo_sessions
    from backend.session_context import get_request_session_id
    db, home_id = request.app.state.db, request.state.home["id"]
    ensure_zones(db, home_id)
    result = {row["id"]: {"id": row["id"], "name": row["name"] or row["id"]} for row in db.all(
        "SELECT DISTINCT d.attributes->>'area_id' AS id,d.attributes->>'area_name' AS name FROM devices d JOIN agents a ON a.id=d.agent_id "
        "WHERE d.home_id=%s AND NOT a.revoked AND d.attributes->>'area_id' IS NOT NULL", (home_id,))}
    for row in db.all("SELECT id,name FROM semantic_zones WHERE home_id=%s", (home_id,)):
        result[row["id"]] = row
    demo = demo_sessions.get(get_request_session_id(request))
    live = request.app.state.home_scopes.get(home_id)
    for scope in (live, demo):
        for c in scope.watering_controllers.list_all():
            room_id = c.zone.zone_id if c.zone else "unassigned"
            result[room_id] = {"id": room_id, "name": c.zone.display_name if c.zone else "Ohne Raum"}
    return sorted(result.values(), key=lambda row: row["name"].lower()) or [{"id": "unassigned", "name": "Ohne Raum"}]


@router.post("/zones")
def create_zone(body: ZoneCreate, request: Request):
    zone_id = uid()
    request.app.state.db.execute("INSERT INTO semantic_zones(home_id,id,name) VALUES (%s,%s,%s)",
                                 (request.state.home["id"], zone_id, body.name))
    return {"id": zone_id, "name": body.name}


@router.get("/devices/{device_id}")
def get_device(device_id: str, request: Request):
    row = request.app.state.db.device(request.state.home["id"], device_id)
    if not row:
        raise HTTPException(404, "Unknown device")
    row["capabilities"] = infer_capabilities(row["entity_id"], row["attributes"])
    return row


@router.post("/devices/{device_id}/actions")
async def device_action(device_id: str, req: DeviceAction, request: Request):
    db, home_id = request.app.state.db, request.state.home["id"]
    device = db.device(home_id, device_id)
    if not device:
        raise HTTPException(404, "Unknown device")
    if not device["available"] or device["state"] in {"unknown", "unavailable"}:
        raise HTTPException(409, "Device unavailable")
    for row in db.all("SELECT spec,deployed_spec,requested FROM automations WHERE home_id=%s AND status<>'deleted'", (home_id,)):
        if any(s and s["enabled"] and device_id in s["targets"] for s in [row["deployed_spec"], row["spec"] if row["requested"] else None]):
            raise HTTPException(409, "Pause the automation before controlling this device manually")
    actions = infer_capabilities(device["entity_id"], device["attributes"]).get("actions", [])
    if req.action not in actions:
        raise HTTPException(422, "Unsupported device action")
    data = {"entity_id": device["entity_id"]}
    service = req.action
    if service == "set_brightness":
        if req.brightness_pct is None:
            raise HTTPException(422, "Select brightness")
        service, data["brightness_pct"] = "turn_on", req.brightness_pct
    ack = await request.app.state.agent_hub.send_command_and_wait_ack(
        agent_id=device["agent_id"], name="ha.call_service",
        args={"domain": device["entity_id"].split(".")[0], "service": service, "data": data}, timeout_s=8)
    return {"accepted": ack.ok, "error": ack.error, "note": "State is confirmed by the next device update"}


@router.get("/automations")
def automations(request: Request, controller_id: str | None = None):
    return request.app.state.db.all("SELECT * FROM automations WHERE home_id=%s AND status<>'deleted'" +
                                   (" AND controller_id=%s" if controller_id else "") + " ORDER BY updated_at DESC",
                                   (request.state.home["id"], controller_id) if controller_id else (request.state.home["id"],))


@router.post("/automations/preview")
def preview(spec: AutomationSpec, request: Request):
    return request.app.state.automations.preview(request.state.home, spec)


def _save(request, body, rule_id=None):
    db, home_id = request.app.state.db, request.state.home["id"]
    existing = db.rule(home_id, rule_id) if rule_id else None
    controller_id = body.controller_id or (existing["controller_id"] if existing else None)
    if existing and controller_id != existing["controller_id"]:
        raise HTTPException(409, "A rule cannot be moved to another controller")
    if controller_id:
        check_rule_binding(get_controller(db, home_id, controller_id), body.spec)
    _, _, agent_id = request.app.state.automations.bindings(home_id, body.spec)
    if rule_id:
        if existing and ((existing["deployed_spec"] and existing["deployed_spec"]["enabled"]) or (existing["requested"] and existing["status"] != "paused")):
            raise HTTPException(409, "Pause the automation before editing its devices or parameters")
        if body.expected_version is None:
            raise HTTPException(422, "Expected version is required when editing")
        updated = db.execute("UPDATE automations SET spec=%s,version=version+1,status='draft',requested=false,error=NULL,updated_at=now() "
                             "WHERE home_id=%s AND id=%s AND version=%s AND NOT deleted AND agent_id=%s",
                             (Jsonb(body.spec.model_dump()), home_id, rule_id, body.expected_version, agent_id))
        if not updated:
            raise HTTPException(409, "Rule changed, missing, or belongs to another connection. Reload before editing.")
    else:
        rule_id = uid()
        controller_id = controller_id or create_from_spec(db, home_id, body.spec, agent_id)
        db.execute("INSERT INTO automations(id,home_id,agent_id,spec,controller_id) VALUES (%s,%s,%s,%s,%s)", (rule_id, home_id, agent_id, Jsonb(body.spec.model_dump()), controller_id))
    db.event(home_id, rule_id, "saved", {})
    return db.rule(home_id, rule_id)


@router.post("/automations")
async def create_automation(body: SaveAutomation, request: Request):
    async with request.app.state.automations._locks.setdefault("controllers:" + request.state.home["id"], asyncio.Lock()):
        return _save(request, body)


@router.put("/automations/{rule_id}")
async def edit_automation(rule_id: str, body: SaveAutomation, request: Request):
    async with request.app.state.automations._locks.setdefault("controllers:" + request.state.home["id"], asyncio.Lock()):
        async with request.app.state.automations._locks.setdefault(rule_id, asyncio.Lock()):
            return _save(request, body, rule_id)


@router.post("/automations/{rule_id}/{operation}")
async def operate_automation(rule_id: str, operation: str, request: Request):
    db, home_id, service = request.app.state.db, request.state.home["id"], request.app.state.automations
    async with service._locks.setdefault("controllers:" + home_id, asyncio.Lock()), service._locks.setdefault(rule_id, asyncio.Lock()):
        rule = db.rule(home_id, rule_id)
        if not rule or rule["deleted"]:
            raise HTTPException(404, "Unknown automation")
        if operation not in {"deploy", "pause", "resume"}:
            raise HTTPException(404, "Unknown operation")
        spec = AutomationSpec.model_validate(rule["spec"])
        if operation != "deploy":
            spec.enabled = operation == "resume"
            service.check_conflicts(home_id, rule_id, spec)
            db.execute("UPDATE automations SET spec=%s,version=version+1 WHERE home_id=%s AND id=%s",
                       (Jsonb(spec.model_dump()), home_id, rule_id))
        else:
            service.check_conflicts(home_id, rule_id, spec)
        db.execute("UPDATE automations SET requested=true,status='pending',updated_at=now() WHERE home_id=%s AND id=%s", (home_id, rule_id))
    return await service.deploy(home_id, rule_id)


@router.delete("/automations/{rule_id}")
async def delete_automation(rule_id: str, request: Request):
    db, home_id, service = request.app.state.db, request.state.home["id"], request.app.state.automations
    async with service._locks.setdefault("controllers:" + home_id, asyncio.Lock()), service._locks.setdefault(rule_id, asyncio.Lock()):
        rule = db.rule(home_id, rule_id)
        if not rule:
            raise HTTPException(404, "Unknown automation")
        if rule["deployed_version"] is None and not rule["requested"]:
            # A draft that never reached HA can be removed while disconnected.
            db.execute("UPDATE automations SET deleted=true,status='deleted',version=version+1 WHERE home_id=%s AND id=%s", (home_id, rule_id))
            db.event(home_id, rule_id, "deleted", {"local_only": True})
            return db.rule(home_id, rule_id)
        db.execute("UPDATE automations SET deleted=true,requested=true,status='pending',version=version+1 WHERE home_id=%s AND id=%s", (home_id, rule_id))
    return await service.deploy(home_id, rule_id)


@router.get("/automation-events")
def events(request: Request, limit: int = Query(default=30, ge=1, le=100), controller_id: str | None = None):
    return request.app.state.db.all("SELECT e.id,e.automation_id,e.kind,e.detail,e.created_at,a.spec->>'name' AS name FROM automation_events e "
                                  "LEFT JOIN automations a ON a.id=e.automation_id WHERE e.home_id=%s" +
                                  (" AND a.controller_id=%s" if controller_id else "") + " ORDER BY e.id DESC LIMIT %s",
                                  (request.state.home["id"], controller_id, limit) if controller_id else (request.state.home["id"], limit))
