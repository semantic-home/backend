CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY, email text NOT NULL UNIQUE, password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS homes (
    id text PRIMARY KEY, name text NOT NULL, timezone text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS memberships (
    home_id text REFERENCES homes(id) ON DELETE CASCADE,
    user_id text REFERENCES users(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('owner','editor','viewer')),
    PRIMARY KEY(home_id,user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY, user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_attempts (
    key text PRIMARY KEY, attempts integer NOT NULL, expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id text PRIMARY KEY, home_id text NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    name text NOT NULL, token_hash text NOT NULL, revoked boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}', UNIQUE(id,home_id)
);
CREATE TABLE IF NOT EXISTS devices (
    id text PRIMARY KEY, home_id text NOT NULL, agent_id text NOT NULL,
    entity_id text NOT NULL, state text NOT NULL, attributes jsonb NOT NULL,
    available boolean NOT NULL DEFAULT true, seen_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(agent_id,home_id) REFERENCES agents(id,home_id) ON DELETE CASCADE,
    UNIQUE(home_id,agent_id,entity_id), UNIQUE(id,home_id)
);
CREATE INDEX IF NOT EXISTS devices_home_idx ON devices(home_id,agent_id,entity_id);
CREATE TABLE IF NOT EXISTS automations (
    id text PRIMARY KEY, home_id text NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    agent_id text NOT NULL, spec jsonb NOT NULL, version integer NOT NULL DEFAULT 1,
    deployed_version integer, deployed_spec jsonb, status text NOT NULL DEFAULT 'draft', error text,
    requested boolean NOT NULL DEFAULT false, deleted boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(agent_id,home_id) REFERENCES agents(id,home_id)
);
CREATE INDEX IF NOT EXISTS automations_home_idx ON automations(home_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS automation_events (
    id bigserial PRIMARY KEY, home_id text NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    automation_id text REFERENCES automations(id) ON DELETE CASCADE,
    kind text NOT NULL, detail jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS automation_events_home_idx ON automation_events(home_id,id DESC);
CREATE TABLE IF NOT EXISTS legacy_objects (
    home_id text REFERENCES homes(id) ON DELETE CASCADE, kind text NOT NULL,
    id text NOT NULL, payload jsonb NOT NULL, PRIMARY KEY(home_id,kind,id)
);
