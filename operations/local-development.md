# Backend Local Development

## Prerequisites

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)

## Setup

```bash
cp .env.example .env
uv sync
```

## Run

```bash
uv run uvicorn --factory backend.__main__:create_app --reload
```

By default the API is exposed under `/api/v1`.

If you want seeded demo controllers and entities, use:

```bash
APP_ENV=dev uv run uvicorn --factory backend.__main__:create_app --reload
```

## Health and Docs

- health check: `/api/v1/health`
- API docs: `/api/v1/scalar`

## Common Checks

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/domains/watering/controllers
curl http://localhost:8000/api/v1/scalar
```

For rule work, also verify:

- `GET /domains/watering/controllers/{controller_id}/why`
- `GET /domains/watering/controllers/{controller_id}/next`
- `GET /domains/watering/controllers/{controller_id}/rule`
- `POST /domains/watering/controllers/{controller_id}/rule/preview`
- `PUT /domains/watering/controllers/{controller_id}/rule`

## Tests

```bash
uv run python -m unittest discover -s tests
```
