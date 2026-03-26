from __future__ import annotations

from fastapi import APIRouter

seed_router = APIRouter()


@seed_router.post("/seed/demo")
async def seed_demo() -> dict:
    from backend.__main__ import clear_all_stores, populate_demo_data, entity_store

    clear_all_stores()
    populate_demo_data()
    return {
        "seeded": True,
        "entity_count": len(entity_store.list_by_source("seed")),
    }
