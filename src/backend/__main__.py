from __future__ import annotations
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict


from fastapi import FastAPI


from .agent_hub.agent_hub import AgentHub
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
from .settings.config import settings
from .platform.database import Database
from .platform.legacy import HomeScopes, ScopedProxy
from .platform.security import HouseholdMiddleware, home_context
from .platform.service import AutomationService
from .platform.routes import router as platform_router

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
async def lifespan(app: FastAPI):
    global home_scopes
    db = app.state.db
    await asyncio.to_thread(db.open)
    runtime_connection = await asyncio.to_thread(db.acquire_runtime_lock)
    home_scopes = HomeScopes(db, agent_hub)
    app.state.home_scopes = home_scopes
    db.execute("UPDATE devices SET available=false")
    for home in db.all("SELECT DISTINCT home_id FROM legacy_objects WHERE kind IN ('controller','rule')"):
        home_scopes.get(home["home_id"])
    cleanup = asyncio.create_task(_run_demo_session_cleanup_loop())
    try:
        yield
    finally:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        await demo_sessions.shutdown()
        await home_scopes.shutdown()
        await agent_hub.shutdown()
        await asyncio.to_thread(db.release_runtime_lock, runtime_connection)
        await asyncio.to_thread(db.close)


def create_app(database_url: str | None = None):

    app = FastAPI(title="HA Backend", lifespan=lifespan, openapi_url=OPENAPI_PATH)
    app.state.db = Database(database_url or settings.database_url)
    app.state.agent_hub = agent_hub
    app.state.automations = AutomationService(app.state.db, agent_hub)
    app.add_middleware(HouseholdMiddleware)
    app.include_router(platform_router, prefix=API_PREFIX, tags=["households", "automations"])
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

home_scopes: HomeScopes
entity_store = ScopedProxy("entity_store")
watering_controllers = ScopedProxy("watering_controllers")
rules_store = ScopedProxy("rules_store")
watering_runtime = ScopedProxy("watering_runtime")
watering_scheduler = ScopedProxy("watering_scheduler")

async def _ha_call_service(agent_id: str, ha_domain: str, ha_service: str, data: Dict[str, Any]) -> None:
    if not home_scopes.db.agent(home_context.get(), agent_id):
        raise RuntimeError("Agent does not belong to this authenticated household")
    cmd = await agent_hub.send_command_and_wait_ack(
        agent_id=agent_id,
        name="ha.call_service",
        args={"domain": ha_domain, "service": ha_service, "data": data},
        timeout_s=10.0,
    )
    if not cmd.ok:
        raise RuntimeError(cmd.error or "Unknown error from agent")


demo_sessions: DemoSessionRegistry = DemoSessionRegistry(service_caller=_ha_call_service)


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
