"""Explicit export/import of existing watering controllers before an upgrade.

Exports never mutate the old server. Imports require the new backend to be stopped,
an explicitly selected household/agent, and matching discovered devices. Imported
rules start paused. Run with --help; credentials come from DATABASE_URL.
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from pydantic_core import to_jsonable_python
from psycopg.types.json import Jsonb

from backend.controller_store.watering import WateringController
from backend.rules_store.watering import Action, Condition, Rule, Schedule
from backend.schemas.zone import Zone
from backend.settings.config import settings
from .database import Database


def export_legacy(url, output, session=None):
    def fetch(path):
        headers = {"X-Semantic-Home-Session": session} if session else {}
        with urlopen(Request(url.rstrip("/") + path, headers=headers), timeout=15) as response:
            return json.load(response)
    payload = {"format": 1, "exported_at": datetime.now(timezone.utc).isoformat(),
               "controllers": fetch("/domains/watering/controllers"), "rules": fetch("/domains/watering/rules")}
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(payload, stream, indent=2)
    return len(payload["controllers"]), len(payload["rules"])


def import_legacy(db, payload, home_id, agent_id):
    if payload.get("format") != 1 or not db.agent(home_id, agent_id):
        raise ValueError("Select a valid exported file and the explicitly intended household connection")
    controllers = []
    for raw in payload["controllers"]:
        names = {field.name for field in fields(WateringController)}
        data = {key: value for key, value in raw.items() if key in names}
        data["agent_id"], data["requires_agent"] = agent_id, True
        if data.get("zone"):
            data["zone"] = Zone.model_validate(data["zone"])
        controller = WateringController(**data)
        for entity_id in controller.actuator_entity_ids + (controller.moisture_sensor_entity_ids or []):
            if not db.one("SELECT 1 FROM devices WHERE home_id=%s AND agent_id=%s AND entity_id=%s", (home_id, agent_id, entity_id)):
                raise ValueError(f"Discover and verify this device before importing: {entity_id}")
        controllers.append(controller)
    controller_ids = {c.controller_id for c in controllers}
    rules = []
    for raw in payload["rules"]:
        if raw["controller_id"] not in controller_ids:
            raise ValueError("A rule references a controller absent from this export")
        rules.append(Rule(controller_id=raw["controller_id"], enabled=raw["enabled"], paused=True,
                          schedule=Schedule.model_validate(raw["schedule"]), action=Action.model_validate(raw["action"]),
                          conditions=[Condition.model_validate(c) for c in raw["conditions"]], version=max(1, raw["version"]),
                          created_at=datetime.fromisoformat(raw["created_at"]) if raw.get("created_at") else datetime.now(timezone.utc),
                          updated_at=datetime.now(timezone.utc)))
    with db.pool.connection() as connection:
        for kind, values in (("controller", controllers), ("rule", rules)):
            for value in values:
                if connection.execute("SELECT 1 FROM legacy_objects WHERE home_id=%s AND kind=%s AND id=%s", (home_id, kind, value.controller_id)).fetchone():
                    raise ValueError("Import would overwrite existing data; choose an empty household or resolve conflicts first")
                data = to_jsonable_python(asdict(value))
                connection.execute("INSERT INTO legacy_objects VALUES (%s,%s,%s,%s)", (home_id, kind, value.controller_id, Jsonb(data)))
    return len(controllers), len(rules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--url", required=True, help="Old API base URL, including /api/v1 if applicable")
    export.add_argument("--output", required=True)
    export.add_argument("--session", help="Optional tab session ID for a session-scoped export")
    restore = commands.add_parser("import")
    restore.add_argument("--input", required=True)
    restore.add_argument("--home-id", required=True)
    restore.add_argument("--agent-id", required=True)
    args = parser.parse_args()
    if args.command == "export":
        count = export_legacy(args.url, args.output, args.session)
    else:
        db = Database(settings.database_url)
        db.open()
        lock = db.acquire_runtime_lock()
        try:
            count = import_legacy(db, json.loads(Path(args.input).read_text()), args.home_id, args.agent_id)
        finally:
            db.release_runtime_lock(lock)
            db.close()
    print(f"Completed: {count[0]} controllers, {count[1]} rules. Imported rules remain paused.")


if __name__ == "__main__":
    main()
