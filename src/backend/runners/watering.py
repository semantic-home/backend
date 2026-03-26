from __future__ import annotations

import asyncio
from typing import Any, Dict

from backend.controller_store.watering import WateringController


async def _ha_call_service(agent_id: str, ha_domain: str, ha_service: str, data: Dict[str, Any]) -> None:
    """
    MVP helper: send a single HA service call via the agent hub command channel.
    """
    from backend.__main__ import agent_hub
    cmd = await agent_hub.send_command_and_wait_ack(
        agent_id=agent_id,
        name="ha.call_service",
        args={"domain": ha_domain, "service": ha_service, "data": data},
        timeout_s=5.0,
    )
    if not cmd.ok:
        raise RuntimeError(cmd.error or "Unknown error from agent")

async def start_watering_for_seconds(controller: WateringController, seconds: int) -> None:
    """
    MVP semantics:
      - turn ALL actuators ON
      - wait N seconds
      - turn ALL actuators OFF
    """
    entity_ids = list(controller.actuator_entity_ids)
    if not entity_ids:
        raise ValueError("WateringController has no actuator_entity_ids configured")

    # Validate domains early (fail fast)
    for entity_id in entity_ids:
        ha_domain = entity_id.split(".", 1)[0]
        if ha_domain not in ("switch", "valve"):
            raise ValueError(f"Unsupported actuator entity domain: {ha_domain} (entity_id={entity_id})")

    # Turn on all
    for entity_id in entity_ids:
        ha_domain = entity_id.split(".", 1)[0]
        await _ha_call_service(
            controller.agent_id, ha_domain, "turn_on", {"entity_id": entity_id}
        )

    try:
        await asyncio.sleep(seconds)
    finally:
        # best-effort off (try all, never raise)
        for entity_id in entity_ids:
            ha_domain = entity_id.split(".", 1)[0]
            try:
                await _ha_call_service(
                    controller.agent_id, ha_domain, "turn_off", {"entity_id": entity_id}
                )
            except Exception:
                pass