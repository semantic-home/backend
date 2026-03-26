from fastapi import APIRouter, FastAPI
from scalar_fastapi import get_scalar_api_reference


def create_docs_router(app: FastAPI) -> APIRouter:
    documentation_router = APIRouter()

    @documentation_router.get(
        "/scalar",
        include_in_schema=False,
    )
    def scalar():
        return get_scalar_api_reference(
            # Keep the OpenAPI path relative so it also works behind the
            # frontend proxy, which exposes the backend under /api.
            openapi_url="openapi.json",
            title=app.title,
        )

    return documentation_router
