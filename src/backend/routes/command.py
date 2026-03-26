from fastapi import APIRouter


from ..schemas.commands import CommandResponse, CommandRequest

command_router = APIRouter()

@command_router.post("/api/command", response_model=CommandResponse)
async def api_command(req: CommandRequest) -> CommandResponse:
    from ..__main__ import agent_hub
    return await agent_hub.send_command_and_wait_ack(
        agent_id=req.agent_id,
        name=req.name,
        args=req.args,
        timeout_s=req.timeout_s,
    )
