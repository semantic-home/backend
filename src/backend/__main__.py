from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict


from fastapi import FastAPI


from .agent_hub.agent_hub import AgentHub
from .controller_store.watering import WateringControllerStore
from .demo_sessions import DemoSessionRegistry
from .entities_store.entities import EntityStore


from .routes.command import command_router
from .routes.documentation import create_docs_router
from .routes.domains.non_watering.controllers import non_watering_router
from .routes.domains.watering.rules import rules_router
from .routes.dynamic.entities import entities_router
from .routes.domains.watering.controllers import watering_router
from .routes.health import health_router
from .routes.seed import seed_router
from .routes.semantic.classify import classify_router
from .routes.ws.agent import agent_router
from .rules_store.watering import RulesStore
from .scheduler.watering import WateringRuleScheduler
from .settings.config import settings
from .watering_fsm import HaServiceActions, WateringRuntimeManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

API_PREFIX = settings.api_prefix or ""
OPENAPI_PATH = f"{API_PREFIX}/openapi.json" if API_PREFIX else "/openapi.json"
DEMO_SESSION_CLEANUP_INTERVAL_S = 3600
DEMO_SESSION_MAX_IDLE_S = 3600


async def _run_demo_session_cleanup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(DEMO_SESSION_CLEANUP_INTERVAL_S)
            cleaned = await demo_sessions.cleanup_idle(max_idle_seconds=DEMO_SESSION_MAX_IDLE_S)
            if cleaned:
                logging.getLogger(__name__).info("Cleaned up %s idle demo session(s).", cleaned)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Demo session cleanup loop failed.")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    watering_scheduler.start()
    demo_session_cleanup_task = asyncio.create_task(_run_demo_session_cleanup_loop())
    try:
        yield
    finally:
        demo_session_cleanup_task.cancel()
        await asyncio.gather(demo_session_cleanup_task, return_exceptions=True)
        await demo_sessions.shutdown()
        await watering_scheduler.shutdown()
        await watering_runtime.shutdown()


def create_app():
    _seed_dev_data_if_enabled()

    app = FastAPI(title="HA Backend", lifespan=lifespan, openapi_url=OPENAPI_PATH)
    app.include_router(health_router, prefix=f"{API_PREFIX}", tags=["health"])
    app.include_router(agent_router, prefix=f"{API_PREFIX}", tags=["ws", "agent"])
    app.include_router(command_router, prefix=f"{API_PREFIX}", tags=["command"])
    app.include_router(entities_router, prefix=f"{API_PREFIX}", tags=["entities"])
    app.include_router(watering_router, prefix=f"{API_PREFIX}", tags=["watering"])
    app.include_router(rules_router, prefix=f"{API_PREFIX}", tags=["rules", "watering"])
    app.include_router(non_watering_router, prefix=f"{API_PREFIX}", tags=["domains"])
    app.include_router(classify_router, prefix=f"{API_PREFIX}", tags=["semantic"])
    app.include_router(seed_router, prefix=f"{API_PREFIX}", tags=["seed"])
    app.include_router(create_docs_router(app), prefix=f"{API_PREFIX}", tags=["documentation"])
    return app


agent_hub: AgentHub = AgentHub()

entity_store: EntityStore = EntityStore()
watering_controllers: WateringControllerStore = WateringControllerStore()
rules_store: RulesStore = RulesStore()
async def _ha_call_service(agent_id: str, ha_domain: str, ha_service: str, data: Dict[str, Any]) -> None:
    cmd = await agent_hub.send_command_and_wait_ack(
        agent_id=agent_id,
        name="ha.call_service",
        args={"domain": ha_domain, "service": ha_service, "data": data},
        timeout_s=300.0,
    )
    if not cmd.ok:
        raise RuntimeError(cmd.error or "Unknown error from agent")


async def _observe_watering_state(entity_id: str, state: str) -> None:
    await watering_runtime.observe_entity_state(entity_id, state)


watering_runtime: WateringRuntimeManager = WateringRuntimeManager(
    actions=HaServiceActions(
        service_caller=_ha_call_service,
        state_observer=_observe_watering_state,
    ),
)
watering_scheduler: WateringRuleScheduler = WateringRuleScheduler(
    rules_store=rules_store,
    controller_store=watering_controllers,
    entity_store=entity_store,
    runtime=watering_runtime,
)
demo_sessions: DemoSessionRegistry = DemoSessionRegistry(service_caller=_ha_call_service)


def clear_all_stores() -> None:
    """Clear every in-memory store (entities, controllers, rules, scheduler, overrides)."""
    from .routes.semantic.classify import clear_classification_overrides

    entity_store.clear()
    watering_controllers.clear()
    rules_store.clear()
    watering_scheduler.clear()
    clear_classification_overrides()


def populate_demo_data(target_store: EntityStore | None = None) -> None:
    """Seed one demo home with entities for the onboarding demo flow."""
    entity_store = target_store or globals()["entity_store"]
    # --- Watering: one pump + one moisture sensor per plant ---
    # Naming convention: switch.{plant}_pump / sensor.{plant}_moisture
    # This lets the frontend group each pair into a per-plant controller suggestion.

    # Living room plants
    entity_store.upsert(
        "switch.living_room_window_sill_monstera_pump",
        "off",
        {"friendly_name": "Living Room Window Sill Monstera Pump", "device_class": "switch", "area_id": "living_room"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.living_room_window_sill_monstera_soil",
        "31",
        {"friendly_name": "Living Room Window Sill Monstera Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "living_room"},
        source="seed",
    )
    entity_store.upsert(
        "switch.living_room_bookshelf_ficus_pump",
        "off",
        {"friendly_name": "Living Room Bookshelf Ficus Pump", "device_class": "switch", "area_id": "living_room"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.living_room_bookshelf_ficus_soil",
        "44",
        {"friendly_name": "Living Room Bookshelf Ficus Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "living_room"},
        source="seed",
    )
    entity_store.upsert(
        "switch.living_room_hanging_pothos_pump",
        "off",
        {"friendly_name": "Living Room Hanging Pothos Pump", "device_class": "switch", "area_id": "living_room"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.living_room_hanging_pothos_soil",
        "38",
        {"friendly_name": "Living Room Hanging Pothos Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "living_room"},
        source="seed",
    )
    entity_store.upsert(
        "switch.living_room_entry_console_zz_plant_pump",
        "off",
        {"friendly_name": "Living Room Entry Console ZZ Plant Pump", "device_class": "switch", "area_id": "living_room"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.living_room_entry_console_zz_plant_soil",
        "52",
        {"friendly_name": "Living Room Entry Console ZZ Plant Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "living_room"},
        source="seed",
    )
    # Bedroom plants
    entity_store.upsert(
        "switch.bedroom_nightstand_calathea_pump",
        "off",
        {"friendly_name": "Bedroom Nightstand Calathea Pump", "device_class": "switch", "area_id": "bedroom"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.bedroom_nightstand_calathea_soil",
        "29",
        {"friendly_name": "Bedroom Nightstand Calathea Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "bedroom"},
        source="seed",
    )
    entity_store.upsert(
        "switch.bedroom_reading_corner_snake_plant_pump",
        "off",
        {"friendly_name": "Bedroom Reading Corner Snake Plant Pump", "device_class": "switch", "area_id": "bedroom"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.bedroom_reading_corner_snake_plant_soil",
        "57",
        {"friendly_name": "Bedroom Reading Corner Snake Plant Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "bedroom"},
        source="seed",
    )
    # Kitchen plants
    entity_store.upsert(
        "switch.kitchen_herb_shelf_basil_pump",
        "off",
        {"friendly_name": "Kitchen Herb Shelf Basil Pump", "device_class": "switch", "area_id": "kitchen"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.kitchen_herb_shelf_basil_soil",
        "22",
        {"friendly_name": "Kitchen Herb Shelf Basil Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "kitchen"},
        source="seed",
    )
    entity_store.upsert(
        "switch.kitchen_sink_side_mint_pump",
        "off",
        {"friendly_name": "Kitchen Sink Side Mint Pump", "device_class": "switch", "area_id": "kitchen"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.kitchen_sink_side_mint_soil",
        "18",
        {"friendly_name": "Kitchen Sink Side Mint Soil Sensor", "unit_of_measurement": "%", "device_class": "moisture", "area_id": "kitchen"},
        source="seed",
    )
    # Shared water tank level sensor
    entity_store.upsert(
        "sensor.home_water_tank_level",
        "76",
        {"friendly_name": "Home Water Tank Level", "unit_of_measurement": "%"},
        source="seed",
    )

    # Lighting entities
    entity_store.upsert(
        "light.living_room_relax_floor_lamp",
        "on",
        {
            "friendly_name": "Living Room Relax Floor Lamp",
            "brightness": 110,
            "color_temp_kelvin": 2400,
        },
        source="seed",
    )
    entity_store.upsert(
        "light.living_room_relax_corner_strip",
        "on",
        {
            "friendly_name": "Living Room Relax Corner Strip",
            "brightness": 92,
            "color_temp_kelvin": 2300,
        },
        source="seed",
    )
    entity_store.upsert(
        "light.living_room_main_ceiling",
        "off",
        {
            "friendly_name": "Living Room Main Ceiling",
            "brightness": 255,
            "color_temp_kelvin": 4000,
        },
        source="seed",
    )
    entity_store.upsert(
        "light.bedroom_sunrise_left",
        "off",
        {
            "friendly_name": "Bedroom Sunrise Left",
            "brightness": 30,
            "color_temp_kelvin": 2200,
        },
        source="seed",
    )
    entity_store.upsert(
        "light.bedroom_sunrise_right",
        "off",
        {
            "friendly_name": "Bedroom Sunrise Right",
            "brightness": 30,
            "color_temp_kelvin": 2200,
        },
        source="seed",
    )
    entity_store.upsert(
        "sensor.living_room_illuminance",
        "18",
        {
            "friendly_name": "Living Room Illuminance",
            "unit_of_measurement": "lx",
            "device_class": "illuminance",
        },
        source="seed",
    )

    # Power entities
    entity_store.upsert(
        "switch.home_office_desk_strip",
        "on",
        {"friendly_name": "Home Office Desk Strip", "device_class": "outlet"},
        source="seed",
    )
    entity_store.upsert(
        "switch.home_office_printer",
        "off",
        {"friendly_name": "Home Office Printer", "device_class": "outlet"},
        source="seed",
    )
    entity_store.upsert(
        "switch.kitchen_coffee_station",
        "off",
        {"friendly_name": "Kitchen Coffee Station", "device_class": "outlet"},
        source="seed",
    )
    entity_store.upsert(
        "switch.bedroom_charger_bank",
        "on",
        {"friendly_name": "Bedroom Charger Bank", "device_class": "outlet"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.home_office_power_total",
        "142",
        {
            "friendly_name": "Home Office Power Total",
            "unit_of_measurement": "W",
            "device_class": "power",
        },
        source="seed",
    )
    entity_store.upsert(
        "sensor.kitchen_power_total",
        "310",
        {
            "friendly_name": "Kitchen Power Total",
            "unit_of_measurement": "W",
            "device_class": "power",
        },
        source="seed",
    )
    entity_store.upsert(
        "sensor.bedroom_power_total",
        "28",
        {
            "friendly_name": "Bedroom Power Total",
            "unit_of_measurement": "W",
            "device_class": "power",
        },
        source="seed",
    )
    entity_store.upsert(
        "sensor.grid_price_current",
        "0.27",
        {
            "friendly_name": "Grid Price Current",
            "unit_of_measurement": "EUR/kWh",
        },
        source="seed",
    )

    # Surveillance entities
    entity_store.upsert(
        "camera.living_room_overview",
        "idle",
        {"friendly_name": "Living Room Overview Camera"},
        source="seed",
    )
    entity_store.upsert(
        "camera.kitchen_entry",
        "recording",
        {"friendly_name": "Kitchen Entry Camera"},
        source="seed",
    )
    entity_store.upsert(
        "binary_sensor.living_room_motion",
        "off",
        {"friendly_name": "Living Room Motion"},
        source="seed",
    )
    entity_store.upsert(
        "binary_sensor.kitchen_motion",
        "on",
        {"friendly_name": "Kitchen Motion"},
        source="seed",
    )
    entity_store.upsert(
        "binary_sensor.bedroom_window_contact",
        "off",
        {"friendly_name": "Bedroom Window Contact", "device_class": "window"},
        source="seed",
    )
    entity_store.upsert(
        "binary_sensor.bathroom_leak",
        "off",
        {"friendly_name": "Bathroom Leak Sensor", "device_class": "moisture"},
        source="seed",
    )
    entity_store.upsert(
        "alarm_control_panel.home_perimeter",
        "disarmed",
        {"friendly_name": "Home Perimeter Alarm"},
        source="seed",
    )
    entity_store.upsert(
        "sensor.surveillance_nvr_storage",
        "68",
        {
            "friendly_name": "NVR Storage Usage",
            "unit_of_measurement": "%",
        },
        source="seed",
    )


def _seed_dev_data_if_enabled() -> None:
    """On startup: clear stores without injecting demo data into the live install flow."""
    clear_all_stores()
