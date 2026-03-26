from __future__ import annotations

import asyncio
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Union

from backend.controller_store.watering import WateringController

logger = logging.getLogger(__name__)

_CONTROLLABLE_DOMAINS: set[str] = {"switch", "valve"}


class WateringMode(str, Enum):
    OFF = "OFF"
    STARTING = "STARTING"
    ON = "ON"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WateringRuntimeState:
    mode: WateringMode = WateringMode.OFF
    run_id: str | None = None
    agent_id: str | None = None
    requires_agent: bool = True
    ttl_s: float | None = None
    targets: tuple[str, ...] = ()
    pending_on: frozenset[str] = field(default_factory=frozenset)
    pending_off: frozenset[str] = field(default_factory=frozenset)
    started_at_s: float | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class StartRequested:
    run_id: str
    agent_id: str
    requires_agent: bool
    ttl_s: float
    targets: tuple[str, ...]


@dataclass(frozen=True)
class StopRequested:
    reason: str = "manual"


@dataclass(frozen=True)
class ActionTimeReached:
    run_id: str


@dataclass(frozen=True)
class ObservedOn:
    entity_id: str


@dataclass(frozen=True)
class ObservedOff:
    entity_id: str


@dataclass(frozen=True)
class Reset:
    reason: str = "manual"


Event = Union[StartRequested, StopRequested, ActionTimeReached, ObservedOn, ObservedOff, Reset]

ServiceCaller = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]


class WateringActions(ABC):
    @abstractmethod
    async def emit_on(
        self,
        *,
        run_id: str,
        agent_id: str,
        targets: tuple[str, ...],
        requires_agent: bool,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def emit_off(
        self,
        *,
        run_id: str,
        agent_id: str,
        targets: tuple[str, ...],
        requires_agent: bool,
    ) -> None:
        raise NotImplementedError


class HaServiceActions(WateringActions):
    """
    Adapter used by the FSM to execute side effects via HA service calls.
    """

    def __init__(
        self,
        service_caller: ServiceCaller,
        *,
        state_observer: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._service_caller = service_caller
        self._state_observer = state_observer

    async def emit_on(
        self,
        *,
        run_id: str,
        agent_id: str,
        targets: tuple[str, ...],
        requires_agent: bool,
    ) -> None:
        if requires_agent:
            await self._emit(agent_id=agent_id, targets=targets, service="turn_on")
            return
        await self._observe(targets=targets, state="on")

    async def emit_off(
        self,
        *,
        run_id: str,
        agent_id: str,
        targets: tuple[str, ...],
        requires_agent: bool,
    ) -> None:
        if requires_agent:
            await self._emit(agent_id=agent_id, targets=targets, service="turn_off")
            return
        await self._observe(targets=targets, state="off")

    async def _emit(self, *, agent_id: str, targets: tuple[str, ...], service: str) -> None:
        if not targets:
            raise RuntimeError("No actuator targets configured")

        calls: list[Awaitable[None]] = []
        for entity_id in targets:
            domain = entity_id.split(".", 1)[0]
            if domain not in _CONTROLLABLE_DOMAINS:
                raise RuntimeError(f"Unsupported actuator entity domain: {domain} (entity_id={entity_id})")
            calls.append(
                self._service_caller(
                    agent_id,
                    domain,
                    service,
                    {"entity_id": entity_id},
                )
            )

        results = await asyncio.gather(*calls, return_exceptions=True)
        failures = [r for r in results if isinstance(r, Exception)]
        if failures:
            raise RuntimeError(f"Failed to call HA service '{service}' for {len(failures)} target(s)") from failures[0]

    async def _observe(self, *, targets: tuple[str, ...], state: str) -> None:
        if self._state_observer is None:
            raise RuntimeError("No runtime state observer configured for simulated controller actions")

        for entity_id in targets:
            await self._state_observer(entity_id, state)


async def transition(
    state: WateringRuntimeState,
    event: Event,
    actions: WateringActions,
) -> WateringRuntimeState:
    match event:
        case Reset(reason=_):
            if state.run_id and state.agent_id and state.targets:
                try:
                    await actions.emit_off(
                        run_id=state.run_id,
                        agent_id=state.agent_id,
                        targets=state.targets,
                        requires_agent=state.requires_agent,
                    )
                except Exception:
                    logger.exception("Reset could not turn targets off.")
            return WateringRuntimeState(mode=WateringMode.OFF)

        case StartRequested(
            run_id=run_id,
            agent_id=agent_id,
            requires_agent=requires_agent,
            ttl_s=ttl_s,
            targets=targets,
        ):
            if state.mode is not WateringMode.OFF:
                raise RuntimeError(f"illegal: Start requested while not OFF (mode={state.mode.value})")
            if ttl_s <= 0:
                raise RuntimeError("ttl_s must be > 0")
            if not targets:
                raise RuntimeError("WateringController has no actuator_entity_ids configured")

            await actions.emit_on(
                run_id=run_id,
                agent_id=agent_id,
                targets=targets,
                requires_agent=requires_agent,
            )
            return WateringRuntimeState(
                mode=WateringMode.STARTING,
                run_id=run_id,
                agent_id=agent_id,
                requires_agent=requires_agent,
                ttl_s=ttl_s,
                targets=targets,
                pending_on=frozenset(targets),
            )

        case ObservedOn(entity_id=entity_id):
            if state.mode is not WateringMode.STARTING:
                return state
            if entity_id not in state.pending_on:
                return state

            remaining = state.pending_on - {entity_id}
            if remaining:
                return replace(state, pending_on=remaining)
            return replace(
                state,
                mode=WateringMode.ON,
                pending_on=frozenset(),
                started_at_s=time.monotonic(),
            )

        case StopRequested(reason=_):
            if state.mode not in (WateringMode.STARTING, WateringMode.ON):
                return state
            if not state.run_id or not state.agent_id:
                return WateringRuntimeState(mode=WateringMode.OFF)

            await actions.emit_off(
                run_id=state.run_id,
                agent_id=state.agent_id,
                targets=state.targets,
                requires_agent=state.requires_agent,
            )
            return replace(
                state,
                mode=WateringMode.STOPPING,
                pending_on=frozenset(),
                pending_off=frozenset(state.targets),
            )

        case ActionTimeReached(run_id=run_id):
            if state.mode not in (WateringMode.STARTING, WateringMode.ON):
                return state
            if state.run_id != run_id:
                return state
            if not state.run_id or not state.agent_id:
                return WateringRuntimeState(mode=WateringMode.OFF)

            await actions.emit_off(
                run_id=state.run_id,
                agent_id=state.agent_id,
                targets=state.targets,
                requires_agent=state.requires_agent,
            )
            return replace(
                state,
                mode=WateringMode.STOPPING,
                pending_on=frozenset(),
                pending_off=frozenset(state.targets),
            )

        case ObservedOff(entity_id=entity_id):
            if state.mode is WateringMode.STOPPING:
                if entity_id not in state.pending_off:
                    return state
                remaining = state.pending_off - {entity_id}
                if remaining:
                    return replace(state, pending_off=remaining)
                return WateringRuntimeState(mode=WateringMode.OFF)

            # Self-heal path: actuator switched off while we expected STARTING/ON.
            if state.mode in (WateringMode.STARTING, WateringMode.ON) and entity_id in state.targets:
                logger.warning(
                    "Actuator %s switched OFF unexpectedly while mode=%s; forcing runtime state to OFF.",
                    entity_id,
                    state.mode.value,
                )
                return WateringRuntimeState(mode=WateringMode.OFF)
            return state

        case _:
            raise RuntimeError(f"unhandled event: {type(event).__name__}")


@dataclass
class _ControllerRuntime:
    controller_id: str
    q: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(maxsize=10_000))
    state: WateringRuntimeState = field(default_factory=WateringRuntimeState)
    known_targets: frozenset[str] = field(default_factory=frozenset)
    start_enqueued: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    loop_task: asyncio.Task | None = None
    auto_stop_task: asyncio.Task | None = None


class WateringRuntimeManager:
    """
    Runtime manager holding one serialized FSM queue per watering controller.
    """

    def __init__(self, actions: WateringActions) -> None:
        self._actions = actions
        self._runtimes: dict[str, _ControllerRuntime] = {}
        self._lock = asyncio.Lock()

    async def start(self, controller: WateringController, *, seconds: int) -> str:
        async with self._lock:
            runtime = self._get_or_create_runtime_locked(controller.controller_id)
            current = runtime.state
            requested_targets = frozenset(controller.actuator_entity_ids)

            for other_controller_id, other_runtime in self._runtimes.items():
                if other_controller_id == controller.controller_id:
                    continue

                other_busy = other_runtime.start_enqueued or (
                    other_runtime.state.mode in (WateringMode.STARTING, WateringMode.ON, WateringMode.STOPPING)
                )
                if not other_busy:
                    continue

                other_targets = other_runtime.known_targets or frozenset(other_runtime.state.targets)
                overlap = requested_targets.intersection(other_targets)
                if overlap:
                    raise RuntimeError(
                        f"Controller '{controller.controller_id}' is busy (shared_targets={sorted(overlap)})"
                    )

            if runtime.start_enqueued:
                raise RuntimeError(
                    f"Controller '{controller.controller_id}' is busy (mode=STARTING, run_id={current.run_id})"
                )
            if current.mode is not WateringMode.OFF and current.mode is not WateringMode.FAILED:
                raise RuntimeError(
                    f"Controller '{controller.controller_id}' is busy (mode={current.mode.value}, run_id={current.run_id})"
                )

            run_id = str(uuid.uuid4())
            event = StartRequested(
                run_id=run_id,
                agent_id=controller.agent_id,
                requires_agent=controller.requires_agent,
                ttl_s=float(seconds),
                targets=tuple(controller.actuator_entity_ids),
            )
            runtime.known_targets = frozenset(controller.actuator_entity_ids)
            try:
                if current.mode is WateringMode.FAILED:
                    runtime.q.put_nowait(Reset(reason="recover-before-start"))
                runtime.q.put_nowait(event)
                runtime.start_enqueued = True
            except asyncio.QueueFull as exc:
                raise RuntimeError("event queue full") from exc
        return run_id

    async def stop(self, controller_id: str) -> str | None:
        async with self._lock:
            runtime = self._runtimes.get(controller_id)
            if runtime is None:
                return None
            rid = runtime.state.run_id
            if rid is None:
                return None
            try:
                runtime.q.put_nowait(StopRequested(reason="manual"))
            except asyncio.QueueFull as exc:
                raise RuntimeError("event queue full") from exc
        return rid

    async def observe_entity_state(self, entity_id: str, state: str) -> None:
        normalized = state.strip().lower()
        if normalized not in ("on", "off"):
            return

        async with self._lock:
            runtimes = list(self._runtimes.values())

        for runtime in runtimes:
            snapshot = runtime.state
            if entity_id not in snapshot.targets and entity_id not in runtime.known_targets:
                continue

            try:
                if normalized == "on":
                    runtime.q.put_nowait(ObservedOn(entity_id=entity_id))
                else:
                    runtime.q.put_nowait(ObservedOff(entity_id=entity_id))
            except asyncio.QueueFull:
                logger.warning(
                    "Dropping %s observation for controller=%s because runtime queue is full.",
                    normalized.upper(),
                    runtime.controller_id,
                )

    def get_state(self, controller_id: str) -> WateringRuntimeState | None:
        runtime = self._runtimes.get(controller_id)
        if runtime is None:
            return None
        return runtime.state

    def get_state_view(self, controller_id: str) -> dict[str, Any]:
        state = self.get_state(controller_id) or WateringRuntimeState()
        return {
            "controller_id": controller_id,
            "mode": state.mode.value,
            "run_id": state.run_id,
            "ttl_s": state.ttl_s,
            "targets": list(state.targets),
            "pending_on": sorted(state.pending_on),
            "pending_off": sorted(state.pending_off),
            "started_at_s": state.started_at_s,
            "last_error": state.last_error,
        }

    async def shutdown(self) -> None:
        async with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()

        await asyncio.gather(*(self._shutdown_runtime(rt) for rt in runtimes), return_exceptions=True)

    async def delete(self, controller_id: str) -> None:
        async with self._lock:
            runtime = self._runtimes.pop(controller_id, None)

        if runtime is None:
            return

        await self._shutdown_runtime(runtime)

    def _get_or_create_runtime_locked(self, controller_id: str) -> _ControllerRuntime:
        runtime = self._runtimes.get(controller_id)
        if runtime is None:
            runtime = _ControllerRuntime(controller_id=controller_id)
            runtime.loop_task = asyncio.create_task(self._event_loop(runtime), name=f"watering-{controller_id}")
            self._runtimes[controller_id] = runtime
        return runtime

    async def _shutdown_runtime(self, runtime: _ControllerRuntime) -> None:
        runtime.stop.set()
        self._cancel_auto_stop(runtime)
        if runtime.loop_task is None:
            return
        try:
            runtime.q.put_nowait(Reset(reason="shutdown"))
        except asyncio.QueueFull:
            pass
        try:
            await asyncio.wait_for(runtime.loop_task, timeout=1.0)
        except asyncio.TimeoutError:
            runtime.loop_task.cancel()
            try:
                await runtime.loop_task
            except asyncio.CancelledError:
                pass

    async def _event_loop(self, runtime: _ControllerRuntime) -> None:
        while not runtime.stop.is_set():
            event = await runtime.q.get()
            before = runtime.state
            try:
                runtime.state = await transition(runtime.state, event, self._actions)
                self._sync_auto_stop(runtime, before, runtime.state)
            except Exception as exc:
                logger.exception(
                    "Watering FSM failed for controller=%s event=%s state=%s",
                    runtime.controller_id,
                    type(event).__name__,
                    before,
                )
                runtime.state = replace(
                    before,
                    mode=WateringMode.FAILED,
                    last_error=str(exc),
                )
                self._cancel_auto_stop(runtime)
            finally:
                if isinstance(event, StartRequested):
                    runtime.start_enqueued = False
                runtime.q.task_done()

    def _sync_auto_stop(
        self,
        runtime: _ControllerRuntime,
        before: WateringRuntimeState,
        after: WateringRuntimeState,
    ) -> None:
        if after.mode is WateringMode.ON and after.run_id and after.ttl_s is not None:
            should_restart = (
                runtime.auto_stop_task is None
                or runtime.auto_stop_task.done()
                or before.run_id != after.run_id
            )
            if should_restart:
                self._cancel_auto_stop(runtime)
                runtime.auto_stop_task = asyncio.create_task(
                    self._enqueue_auto_stop(runtime, after.run_id, after.ttl_s),
                    name=f"watering-auto-stop-{runtime.controller_id}",
                )
            return

        if after.mode in (WateringMode.STARTING, WateringMode.STOPPING, WateringMode.OFF, WateringMode.FAILED):
            self._cancel_auto_stop(runtime)

    def _cancel_auto_stop(self, runtime: _ControllerRuntime) -> None:
        if runtime.auto_stop_task is None:
            return
        if not runtime.auto_stop_task.done():
            runtime.auto_stop_task.cancel()
        runtime.auto_stop_task = None

    async def _enqueue_auto_stop(self, runtime: _ControllerRuntime, run_id: str, ttl_s: float) -> None:
        try:
            await asyncio.sleep(ttl_s)
        except asyncio.CancelledError:
            return

        if runtime.stop.is_set():
            return
        try:
            runtime.q.put_nowait(ActionTimeReached(run_id=run_id))
        except asyncio.QueueFull:
            logger.warning("Failed to enqueue ActionTimeReached for controller=%s (queue full).", runtime.controller_id)
