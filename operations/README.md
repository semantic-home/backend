# Backend Operations

Public operations notes for this service. These documents describe how the backend is run, checked, and packaged from the perspective of another engineer opening the repository.

## Runtime Model

- FastAPI application served with Uvicorn
- watering scheduler runs inside the same process as the API
- watering runtime executes Home Assistant service calls through connected agents
- in-memory stores are used for entities, controllers, rules, and scheduler state

## Documents

- [Local Development](./local-development.md)
- [Deployment](./deployment.md)
- [Release Checklist](./release-checklist.md)

## Constraints

- restarting the service clears in-memory runtime and scheduler state
- demo seed data is session-scoped rather than startup-seeded
- no dedicated background worker or persistent database is configured in this repo
- scheduler timing and API availability currently share the same process lifecycle
