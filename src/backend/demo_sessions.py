from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from backend.controller_store.watering import WateringControllerStore
from backend.entities_store.entities import EntityStore
from backend.rules_store.watering import RulesStore
from backend.scheduler.watering import WateringRuleScheduler
from backend.watering_fsm import HaServiceActions, WateringRuntimeManager

ServiceCaller = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]


@dataclass
class DemoSessionState:
    session_id: str
    _service_caller: ServiceCaller = field(repr=False)
    entity_store: EntityStore = field(default_factory=EntityStore)
    watering_controllers: WateringControllerStore = field(default_factory=WateringControllerStore)
    rules_store: RulesStore = field(default_factory=RulesStore)
    classification_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    watering_runtime: WateringRuntimeManager = field(init=False)
    watering_scheduler: WateringRuleScheduler = field(init=False)
    last_touched_at: datetime = field(init=False)

    def __post_init__(self) -> None:
        async def observe(entity_id: str, state: str) -> None:
            existing = self.entity_store.get(entity_id)
            attributes = existing.attributes if existing is not None else {}
            source = existing.source if existing is not None else "seed"
            source_id = existing.source_id if existing is not None else self.session_id
            self.entity_store.upsert(
                entity_id,
                state,
                attributes,
                source=source,
                source_id=source_id,
            )
            await self.watering_runtime.observe_entity_state(entity_id, state)

        self.watering_runtime = WateringRuntimeManager(
            actions=HaServiceActions(
                service_caller=self._service_caller,
                state_observer=observe,
            ),
        )
        self.watering_scheduler = WateringRuleScheduler(
            rules_store=self.rules_store,
            controller_store=self.watering_controllers,
            entity_store=self.entity_store,
            runtime=self.watering_runtime,
        )
        self.watering_scheduler.start()
        self.touch()

    def touch(self) -> None:
        self.last_touched_at = datetime.now(timezone.utc)

    def has_demo_entities(self) -> bool:
        return bool(self.entity_store.list_by_source("seed"))

    def has_controllers(self) -> bool:
        return bool(self.watering_controllers.list_all())

    def has_rules(self) -> bool:
        return bool(self.rules_store.list_all())

    async def reset(self) -> None:
        controllers = list(self.watering_controllers.list_all())
        for controller in controllers:
            await self.watering_runtime.delete(controller.controller_id)
        self.entity_store.clear()
        self.watering_controllers.clear()
        self.rules_store.clear()
        self.watering_scheduler.clear()
        self.classification_overrides.clear()
        self.touch()

    async def shutdown(self) -> None:
        await self.watering_scheduler.shutdown()
        await self.watering_runtime.shutdown()


class DemoSessionRegistry:
    def __init__(self, *, service_caller: ServiceCaller) -> None:
        self._service_caller = service_caller
        self._sessions: dict[str, DemoSessionState] = {}

    def get(self, session_id: str) -> DemoSessionState:
        session = self._sessions.get(session_id)
        if session is None:
            session = DemoSessionState(session_id=session_id, _service_caller=self._service_caller)
            self._sessions[session_id] = session
        session.touch()
        return session

    async def cleanup_idle(self, *, max_idle_seconds: float) -> int:
        now = datetime.now(timezone.utc)
        expired_session_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if (now - session.last_touched_at).total_seconds() >= max_idle_seconds
        ]
        if not expired_session_ids:
            return 0

        sessions = [
            self._sessions.pop(session_id)
            for session_id in expired_session_ids
            if session_id in self._sessions
        ]
        await asyncio.gather(*(session.shutdown() for session in sessions), return_exceptions=True)
        return len(sessions)

    async def shutdown(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        await asyncio.gather(*(session.shutdown() for session in sessions), return_exceptions=True)
