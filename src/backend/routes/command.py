from fastapi import APIRouter, HTTPException, Request

from backend.platform.models import DeviceAction
from backend.platform.routes import device_action
from backend.schemas.commands import CommandRequest

command_router = APIRouter()


@command_router.post("/api/command")
async def api_command(req: CommandRequest, request: Request):
    # Keep the legacy endpoint, but give it the same ownership, action allowlist
    # and active-automation checks as the public device endpoint.
    if req.name != "ha.call_service":
        raise HTTPException(403, "Use the validated automation API for configuration")
    args = req.args
    data = args.get("data", {})
    if not isinstance(data, dict) or set(data) - {"entity_id", "brightness_pct"}:
        raise HTTPException(422, "Explicit single-entity actions are required")
    device = request.app.state.db.one(
        "SELECT id,entity_id FROM devices WHERE home_id=%s AND agent_id=%s AND entity_id=%s",
        (request.state.home["id"], req.agent_id, data.get("entity_id")))
    if not device or device["entity_id"].split(".")[0] != args.get("domain"):
        raise HTTPException(404, "Unknown device connection")
    action = "set_brightness" if "brightness_pct" in data else args.get("service")
    result = await device_action(device["id"], DeviceAction(action=action, brightness_pct=data.get("brightness_pct")), request)
    return {"id": device["id"], "ok": result["accepted"], "error": result["error"]}
