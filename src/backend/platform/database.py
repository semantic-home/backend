from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


def uid() -> str:
    return str(uuid4())


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, url: str):
        self.pool = ConnectionPool(url, open=False, min_size=1, max_size=10,
                                   kwargs={"row_factory": dict_row}, timeout=5)

    def open(self):
        self.pool.open(wait=True, timeout=10)
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(73492110)")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_versions (version text PRIMARY KEY)")
            for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
                if not conn.execute("SELECT 1 FROM schema_versions WHERE version=%s", (path.name,)).fetchone():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_versions VALUES (%s)", (path.name,))

    def close(self):
        self.pool.close()

    def acquire_runtime_lock(self):
        connection = self.pool.getconn()
        if not connection.execute("SELECT pg_try_advisory_lock(73492111) AS acquired").fetchone()["acquired"]:
            self.pool.putconn(connection)
            raise RuntimeError("This release requires one backend process per database; another runtime is already active")
        connection.commit()
        return connection

    def release_runtime_lock(self, connection):
        connection.execute("SELECT pg_advisory_unlock(73492111)")
        connection.commit()
        self.pool.putconn(connection)

    def all(self, sql, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    def session(self, user_id):
        token = secrets.token_urlsafe(40)
        self.execute("DELETE FROM sessions WHERE expires_at < now()")
        self.execute("INSERT INTO sessions VALUES (%s,%s,now()+interval '7 days')",
                     (token_hash(token), user_id))
        return token

    def user(self, token):
        return self.one("SELECT u.id,u.email FROM users u JOIN sessions s ON s.user_id=u.id "
                        "WHERE s.token_hash=%s AND s.expires_at > now()", (token_hash(token),))

    def homes(self, user_id):
        return self.all("SELECT h.*,m.role FROM homes h JOIN memberships m ON m.home_id=h.id "
                        "WHERE m.user_id=%s ORDER BY h.created_at,h.id", (user_id,))

    def agent(self, home_id, agent_id):
        return self.one("SELECT id,home_id,name,metadata,revoked FROM agents WHERE id=%s AND home_id=%s AND NOT revoked",
                        (agent_id, home_id))

    def device(self, home_id, device_id):
        return self.one("SELECT d.* FROM devices d JOIN agents a ON a.id=d.agent_id "
                        "WHERE d.home_id=%s AND d.id=%s AND NOT a.revoked", (home_id, device_id))

    def ingest(self, home_id, agent_id, entities, *, snapshot=False):
        with self.pool.connection() as conn:
            if snapshot:
                conn.execute("UPDATE devices SET available=false WHERE home_id=%s AND agent_id=%s", (home_id, agent_id))
            for e in entities:
                registry_id = e.attributes.get("registry_id")
                if registry_id:
                    prior = conn.execute("SELECT id,entity_id FROM devices WHERE home_id=%s AND agent_id=%s AND attributes->>'registry_id'=%s",
                                         (home_id, agent_id, registry_id)).fetchone()
                    occupying = conn.execute("SELECT id,attributes FROM devices WHERE home_id=%s AND agent_id=%s AND entity_id=%s",
                                             (home_id, agent_id, e.entity_id)).fetchone()
                    if occupying and (occupying["attributes"].get("registry_id") not in {None, registry_id}
                                      or (prior and prior["id"] != occupying["id"])):
                        conn.execute("UPDATE devices SET entity_id=%s,available=false WHERE id=%s", ("removed." + occupying["id"].replace("-", ""), occupying["id"]))
                    if prior and prior["entity_id"] != e.entity_id:
                        conn.execute("UPDATE devices SET entity_id=%s WHERE id=%s", (e.entity_id, prior["id"]))
                conn.execute("INSERT INTO devices(id,home_id,agent_id,entity_id,state,attributes) VALUES (%s,%s,%s,%s,%s,%s) "
                             "ON CONFLICT(home_id,agent_id,entity_id) DO UPDATE SET state=excluded.state,attributes=excluded.attributes,"
                             "available=true,seen_at=now()", (uid(), home_id, agent_id, e.entity_id, e.state, Jsonb(e.attributes)))

    def rule(self, home_id, rule_id):
        return self.one("SELECT * FROM automations WHERE home_id=%s AND id=%s", (home_id, rule_id))

    def event(self, home_id, rule_id, kind, detail):
        self.execute("INSERT INTO automation_events(home_id,automation_id,kind,detail) VALUES (%s,%s,%s,%s)",
                     (home_id, rule_id, kind, Jsonb(detail)))
