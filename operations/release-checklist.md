# Backend Release Checklist

Use this before merging or deploying backend changes.

## Verify the Branch

- confirm the branch is up to date with the target branch
- confirm API changes are reflected in schemas and docs where relevant

## Run Checks

```bash
uv run python -m unittest discover -s tests
```

## Smoke Test

With the service running:

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/domains/watering/controllers
curl http://localhost:8000/api/v1/scalar
```

If the change touches rules or scheduling, also verify:

- `why`
- `next`
- `rule`
- `rule preview`
- runtime start / stop behavior as applicable

## Confirm Runtime Assumptions

- `APP_ENV=prod` does not seed development data
- the frontend still reads the expected API payloads
- if the change affects execution, a connected agent is available for end-to-end testing
