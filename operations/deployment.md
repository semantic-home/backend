# Backend Deployment

## Container Build

Build the image from the `backend/` directory:

```bash
docker build -t home-automation-backend .
```

## Standalone Container Smoke Test

```bash
docker run --rm -p 8000:8000 --env-file .env home-automation-backend
```

The container starts the FastAPI application with:

```bash
uvicorn --factory backend.__main__:create_app --host 0.0.0.0 --port 8000
```

This standalone run is useful for smoke testing the backend image directly. The intended alpha deployment is a shared Compose stack with the frontend.


## Environment

- `API_PREFIX=/api/v1`: mounts the API under the versioned prefix
- `SEMANTIC_HOME_BETA_KEY=`: optional shared key required for agent cloud pairing when beta access is enabled

## Operational Notes

- the scheduler runs in the same process as the API service
- a restart clears in-memory entity, rule, runtime, and scheduler state
- real actuator execution depends on a connected agent that can acknowledge Home Assistant service calls
- demo seed data is session-scoped and is loaded explicitly through the onboarding flow or `POST /api/v1/seed/demo`
- this repo does not currently define a separate worker or a persistent database
- for the alpha deployment, frontend-to-backend routing is expected to happen over the shared Compose network
