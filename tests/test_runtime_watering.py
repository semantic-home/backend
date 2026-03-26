from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backend.controller_store.watering import WateringController
from backend.watering_fsm import HaServiceActions, WateringMode, WateringRuntimeManager


class WateringRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.service_calls: list[tuple[str, str, str, dict[str, Any]]] = []
        runtime: WateringRuntimeManager | None = None

        async def service_caller(agent_id: str, ha_domain: str, ha_service: str, data: dict[str, Any]) -> None:
            self.service_calls.append((agent_id, ha_domain, ha_service, data))

        async def state_observer(entity_id: str, state: str) -> None:
            assert runtime is not None
            await runtime.observe_entity_state(entity_id, state)

        self.runtime = WateringRuntimeManager(
            actions=HaServiceActions(
                service_caller=service_caller,
                state_observer=state_observer,
            )
        )
        runtime = self.runtime

    async def asyncTearDown(self) -> None:
        await self.runtime.shutdown()

    async def _wait_for_mode(
        self,
        controller_id: str,
        expected_mode: WateringMode,
        *,
        timeout_s: float = 1.0,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            state = self.runtime.get_state(controller_id)
            if state is not None and state.mode is expected_mode:
                return
            await asyncio.sleep(0.01)
        self.fail(f"Timed out waiting for controller={controller_id} to reach mode={expected_mode.value}")

    async def test_seeded_controller_runs_without_agent_calls(self) -> None:
        controller = WateringController(
            controller_id="demo_controller",
            display_name="Demo Controller",
            actuator_entity_ids=["switch.demo_pump"],
            agent_id="demo-seed",
            requires_agent=False,
        )

        run_id = await self.runtime.start(controller, seconds=5)

        self.assertTrue(run_id)
        await self._wait_for_mode("demo_controller", WateringMode.ON)
        self.assertEqual(self.service_calls, [])

        await self.runtime.stop("demo_controller")
        await self._wait_for_mode("demo_controller", WateringMode.OFF)
        self.assertEqual(self.service_calls, [])

    async def test_agent_backed_controller_still_uses_service_calls(self) -> None:
        controller = WateringController(
            controller_id="real_controller",
            display_name="Real Controller",
            actuator_entity_ids=["switch.real_pump"],
            agent_id="home1",
        )

        run_id = await self.runtime.start(controller, seconds=5)

        self.assertTrue(run_id)
        await self._wait_for_mode("real_controller", WateringMode.STARTING)
        self.assertEqual(
            self.service_calls,
            [("home1", "switch", "turn_on", {"entity_id": "switch.real_pump"})],
        )

    async def test_delete_removes_runtime_state(self) -> None:
        controller = WateringController(
            controller_id="delete_controller",
            display_name="Delete Controller",
            actuator_entity_ids=["switch.delete_pump"],
            agent_id="demo-seed",
            requires_agent=False,
        )

        await self.runtime.start(controller, seconds=5)
        await self._wait_for_mode("delete_controller", WateringMode.ON)

        await self.runtime.delete("delete_controller")

        self.assertIsNone(self.runtime.get_state("delete_controller"))
