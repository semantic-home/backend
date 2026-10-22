# Semantic Home Backend

FastAPI backend for the home automation UI. The service manages domain controllers, evaluates watering rules, schedules due runs, and forwards actuator commands to connected agents.

## Responsibilities

- expose REST and websocket APIs for the frontend and agents
- persist households, memberships, device inventory and automation versions in PostgreSQL
- authenticate browser sessions and individually paired local agents
- compile humidity, switching and cleaning templates into locally executed HA automations
- evaluate watering rules through a policy layer
- schedule due watering rules and execute them through the runtime
- provide explainability endpoints such as `why`, `preview`, and `next`

## Core Concepts

- **Zone**: a user-confirmed room or logical scope, optionally suggested by HA areas
- **Controller**: a meaningful group of entities assigned to roles, belonging to a zone; its child rules define behavior
- **Rule**: a stored schedule, condition set, and action for a controller
- **Policy**: the decision layer that answers whether a rule should run now
- **Scheduler**: the loop that checks due rules and triggers execution
- **Runtime**: the execution layer that starts, stops, and auto-stops watering safely
- **Agent**: the bridge to Home Assistant service calls and entity updates

## Project Layout

- `src/backend/routes/`: HTTP and websocket routes
- `src/backend/controller_store/`: controller storage
- `src/backend/entities_store/`: live entity state storage
- `src/backend/rules_store/`: persisted in-memory rule storage
- `src/backend/policy/`: rule evaluation and preview logic
- `src/backend/scheduler/`: next-run calculation and scheduler loop
- `src/backend/watering_fsm/`: watering runtime and safety guards
- `src/backend/semantic/`: user-facing explanation views such as `why`
- `tests/`: policy and scheduler tests

## Local Development

### Prerequisites

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)

### Setup

```bash
cp .env.example .env
uv sync --group dev
```

### Run

```bash
uv run uvicorn --app-dir src --factory backend.__main__:create_app --reload
```

By default the API is exposed under `/api/v1`.
Demo seed data is session-scoped and loaded explicitly through the frontend onboarding flow or `POST /api/v1/seed/demo`.

## API and Docs

- Health check: `/api/v1/health`
- Scalar API docs: `/api/v1/scalar`

## Testing

```bash
TEST_DATABASE_URL=postgresql://USER:PASSWORD@localhost:55432/semantic_home_test uv run --group dev python -m unittest discover -s tests
```

## Environment

The default environment variables are defined in `.env.example`.

- `API_PREFIX=/api/v1`: mount API routes under a versioned prefix
- `DATABASE_URL=`: PostgreSQL connection string
- `AUTH_COOKIE_SECURE=true`: require HTTPS for session cookies in production
- `REGISTRATION_KEY=`: registration key; required by the production Compose configuration
- `ALLOWED_ORIGINS=`: additional explicitly allowed browser origins
- `SEMANTIC_HOME_BETA_KEY=`: legacy setting; individual agent credentials are now required

## Operations

- the backend runs as a single FastAPI application process
- the watering scheduler runs in-process, not as a separate worker
- household data, device inventory, watering definitions and watering pause/skip state are persistent
- runtime connections and active watering FSMs are process-local; use exactly one backend worker
- a database advisory lock prevents two backend runtimes from using the same database
- legacy beta-key-only agents must be updated and paired with household-specific credentials
- real actuator execution depends on a connected agent that can acknowledge Home Assistant service calls

Public operational notes live in [operations/README.md].

Detailed manual deploy and release notes live in the separate planning repository under `planning/operations/BACKEND_DEPLOY_RELEASE.md`.

## Household automation release

See [the implementation and setup guide](operations/MULTI_HOME_AUTOMATIONS.md). PostgreSQL is required; there is no SQLite fallback. Set DATABASE_URL before startup. Migrations are packaged with the application and applied transactionally at startup. The integration tests clear only an explicitly named semantic_home_test database.

Existing watering definitions should be exported before replacing an old backend. Use `python -m backend.platform.migrate_legacy --help`; the import requires a selected household/agent and a stopped new backend, rejects collisions, and restores rules paused.

The shared controller model and migration are described in [Controllers and zones](operations/CONTROLLERS_AND_ZONES.md). The new domain routes preserve the existing `/domains/{domain}/zones/{zone}/controllers` structure.
