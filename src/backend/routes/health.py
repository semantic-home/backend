from typing import Dict

from fastapi import APIRouter


health_router = APIRouter()

@health_router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}



