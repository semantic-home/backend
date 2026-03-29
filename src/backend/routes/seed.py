from __future__ import annotations

from fastapi import APIRouter, Request

from backend.session_context import get_request_session_id

seed_router = APIRouter()


@seed_router.post("/seed/demo")
async def seed_demo(request: Request) -> dict:
    from backend.__main__ import demo_sessions, populate_demo_data

    session = demo_sessions.get(get_request_session_id(request))
    await session.reset()
    populate_demo_data(target_store=session.entity_store)
    return {
        "seeded": True,
        "entity_count": len(session.entity_store.list_by_source("seed")),
    }
