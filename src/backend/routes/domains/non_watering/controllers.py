from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.schemas.generic_controller import GenericControllerView, GenericWhyView

non_watering_router = APIRouter()

_SUPPORTED_DOMAINS = frozenset({"lighting", "power", "surveillance"})


def _validate_domain(domain: str) -> None:
    if domain not in _SUPPORTED_DOMAINS:
        raise HTTPException(status_code=404, detail=f"Unsupported domain: {domain}")


@non_watering_router.get("/domains/{domain}/controllers", response_model=list[GenericControllerView])
async def list_controllers(domain: str) -> list[GenericControllerView]:
    _validate_domain(domain)
    return []


@non_watering_router.get(
    "/domains/{domain}/zones/{zone_id}/controllers",
    response_model=list[GenericControllerView],
)
async def list_zone_controllers(domain: str, zone_id: str) -> list[GenericControllerView]:
    _validate_domain(domain)
    return []


@non_watering_router.get(
    "/domains/{domain}/zones/{zone_id}/controllers/{controller_id}",
    response_model=GenericControllerView,
)
async def get_controller_per_zone(domain: str, zone_id: str, controller_id: str) -> GenericControllerView:
    _validate_domain(domain)
    raise HTTPException(
        status_code=404,
        detail=f"No controller found under {domain}/{zone_id} with id {controller_id}",
    )


@non_watering_router.get("/domains/{domain}/controllers/{controller_id}/why", response_model=GenericWhyView)
async def get_controller_why(domain: str, controller_id: str) -> GenericWhyView:
    _validate_domain(domain)
    raise HTTPException(status_code=404, detail="Unknown controller_id")
