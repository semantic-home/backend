CREATE TABLE semantic_zones (
    home_id text NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    id text NOT NULL, name text NOT NULL, source text NOT NULL DEFAULT 'user',
    PRIMARY KEY(home_id,id)
);
INSERT INTO semantic_zones(home_id,id,name,source)
SELECT DISTINCT ON (home_id,attributes->>'area_id') home_id,attributes->>'area_id',
    coalesce(attributes->>'area_name',attributes->>'area_id'),'home_assistant'
FROM devices WHERE attributes->>'area_id' IS NOT NULL ORDER BY home_id,attributes->>'area_id',seen_at DESC;
INSERT INTO semantic_zones(home_id,id,name,source)
SELECT DISTINCT ON (home_id,payload->'zone'->>'zone_id') home_id,payload->'zone'->>'zone_id',
    coalesce(payload->'zone'->>'display_name',payload->'zone'->>'zone_id'),'controller'
FROM legacy_objects WHERE kind='controller' AND payload->'zone'->>'zone_id' IS NOT NULL
ON CONFLICT DO NOTHING;
INSERT INTO semantic_zones(home_id,id,name) SELECT id,'unassigned','Nicht zugeordnet' FROM homes ON CONFLICT DO NOTHING;

CREATE TABLE semantic_controllers (
    id text PRIMARY KEY, home_id text NOT NULL REFERENCES homes(id) ON DELETE CASCADE,
    display_name text NOT NULL, domain text NOT NULL CHECK(domain IN ('humidity','lighting','power','cleaning')),
    zone_id text NOT NULL DEFAULT 'unassigned', agent_id text NOT NULL,
    bindings jsonb NOT NULL, version integer NOT NULL DEFAULT 1,
    UNIQUE(id,home_id),
    FOREIGN KEY(home_id,zone_id) REFERENCES semantic_zones(home_id,id),
    FOREIGN KEY(agent_id,home_id) REFERENCES agents(id,home_id)
);
CREATE INDEX semantic_controllers_zone_idx ON semantic_controllers(home_id,domain,zone_id);

-- Preserve the previous draft and deployed rule IDs: each becomes a child of a
-- controller. This migration never deploys, pauses or changes a device command.
INSERT INTO semantic_controllers(id,home_id,display_name,domain,zone_id,agent_id,bindings)
SELECT a.id,a.home_id,a.spec->>'name',
    CASE a.spec->>'kind' WHEN 'humidity_band' THEN 'humidity' WHEN 'cleaning' THEN 'cleaning'
         ELSE CASE WHEN EXISTS(SELECT 1 FROM devices d WHERE d.home_id=a.home_id AND a.spec->'targets' ? d.id AND d.entity_id LIKE 'light.%') THEN 'lighting' ELSE 'power' END END,
    coalesce((SELECT d.attributes->>'area_id' FROM devices d WHERE d.home_id=a.home_id AND d.id=a.spec->'targets'->>0),'unassigned'),
    a.agent_id,jsonb_build_object('targets',a.spec->'targets','sensor_id',a.spec->'sensor_id')
FROM automations a;
ALTER TABLE automations ADD COLUMN controller_id text;
UPDATE automations SET controller_id=id;
ALTER TABLE automations ALTER COLUMN controller_id SET NOT NULL;
ALTER TABLE automations ADD CONSTRAINT automations_controller_fk FOREIGN KEY(controller_id,home_id) REFERENCES semantic_controllers(id,home_id);
CREATE INDEX automations_controller_idx ON automations(home_id,controller_id);
