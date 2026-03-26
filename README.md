# Home Automation Backend

FastAPI backend for the home automation UI. The service manages domain controllers, evaluates watering rules, schedules due runs, and forwards actuator commands to connected agents.

## Responsibilities

- expose REST and websocket APIs for the frontend and agents
- store live entity state and controller definitions in memory
- evaluate watering rules through a policy layer
- schedule due watering rules and execute them through the runtime
- provide explainability endpoints such as `why`, `preview`, and `next`

## Core Concepts

- **Controller**: a user-facing automation unit, such as a watering controller for one zone or plant group
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
uv sync
```

### Run

```bash
uv run uvicorn --factory backend.__main__:create_app --reload
```

By default the API is exposed under `/api/v1`.

If `APP_ENV=dev`, the app seeds demo watering entities and controllers on startup so the frontend can be exercised immediately.

## API and Docs

- Health check: `/api/v1/health`
- Scalar API docs: `/api/v1/scalar`

## Testing

```bash
uv run python -m unittest discover -s tests
```

## Environment

The default environment variables are defined in `.env.example`.

- `APP_ENV=dev`: enable startup seed data for local development
- `APP_ENV=prod`: disable startup seeding
- `API_PREFIX=/api/v1`: mount API routes under a versioned prefix

## Operations

- the backend runs as a single FastAPI application process
- the watering scheduler runs in-process, not as a separate worker
- controller, entity, rule, and scheduler state are currently stored in memory
- real actuator execution depends on a connected agent that can acknowledge Home Assistant service calls

Public operational notes live in [operations/README.md](/Users/iliassaymaz/PycharmProjects/home-automation/backend/operations/README.md).

Detailed manual deploy and release notes live in the separate planning repository under `planning/operations/BACKEND_DEPLOY_RELEASE.md`.
