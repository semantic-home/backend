import asyncio

from fastapi import APIRouter, HTTPException, Request
from psycopg.types.json import Jsonb

from backend.platform.controllers import KINDS, binding_fields, controller_spec, ensure_zones, get_controller, normalized_bindings, view
from backend.platform.database import uid
from backend.platform.models import AutomationSpec, ControllerSave

non_watering_router = APIRouter()


@non_watering_router.get('/domains/{domain}/controllers')
@non_watering_router.get('/domains/{domain}/zones/{zone_id}/controllers')
def list_controllers(domain: str, request: Request, zone_id: str | None = None):
    if domain not in {*KINDS, 'surveillance'}:
        raise HTTPException(404, 'Unsupported domain')
    db, home_id = request.app.state.db, request.state.home['id']
    rows = db.all('SELECT c.*,z.name AS zone_name FROM semantic_controllers c JOIN semantic_zones z ON z.home_id=c.home_id AND z.id=c.zone_id '
                  'WHERE c.home_id=%s AND c.domain=%s' + (' AND c.zone_id=%s' if zone_id else '') + ' ORDER BY c.display_name,c.id',
                  (home_id, domain, zone_id) if zone_id else (home_id, domain))
    return [view(db, c) for c in rows]


@non_watering_router.get('/domains/{domain}/controllers/{controller_id}')
@non_watering_router.get('/domains/{domain}/zones/{zone_id}/controllers/{controller_id}')
def get_controller_route(domain: str, controller_id: str, request: Request, zone_id: str | None = None):
    c = get_controller(request.app.state.db, request.state.home['id'], controller_id)
    if c['domain'] != domain or (zone_id and c['zone_id'] != zone_id):
        raise HTTPException(404, 'Unknown controller in this zone')
    return view(request.app.state.db, c)


@non_watering_router.post('/domains/{domain}/controllers')
@non_watering_router.put('/domains/{domain}/controllers/{controller_id}')
async def save_controller(domain: str, body: ControllerSave, request: Request, controller_id: str | None = None):
    if domain not in KINDS:
        raise HTTPException(404, 'Unsupported controller purpose')
    db, home_id, service = request.app.state.db, request.state.home['id'], request.app.state.automations
    async with service._locks.setdefault('controllers:' + home_id, asyncio.Lock()):
        ensure_zones(db, home_id)
        if not db.one('SELECT id FROM semantic_zones WHERE home_id=%s AND id=%s', (home_id, body.zone_id)):
            raise HTTPException(422, 'Choose a zone from this household')
        proposed = {'display_name': body.display_name, 'domain': domain, 'bindings': binding_fields(body)}
        try:
            spec = controller_spec(proposed)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        _, _, agent_id = service.bindings(home_id, spec)
        if controller_id:
            current = get_controller(db, home_id, controller_id)
            if current['domain'] != domain:
                raise HTTPException(409, 'The controller purpose cannot be changed')
            changed = normalized_bindings(current) != proposed['bindings'] or agent_id != current['agent_id']
            with db.pool.connection() as conn:
                rules = conn.execute("SELECT * FROM automations WHERE home_id=%s AND controller_id=%s AND status<>'deleted' FOR UPDATE", (home_id, controller_id)).fetchall()
                if agent_id != current['agent_id'] and any(r['deployed_version'] is not None for r in rules):
                    raise HTTPException(409, 'Remove locally deployed rules before moving the controller to another connection')
                if changed and any((r['deployed_spec'] and r['deployed_spec']['enabled']) or (r['requested'] and r['status'] != 'paused') for r in rules):
                    raise HTTPException(409, 'Pause the controller rules and wait for confirmation before changing entity roles')
                for rule in rules if changed else []:
                    next_spec = {**rule['spec'], **proposed['bindings']}
                    service.bindings(home_id, AutomationSpec.model_validate(next_spec))
                updated = conn.execute('UPDATE semantic_controllers SET display_name=%s,zone_id=%s,agent_id=%s,bindings=%s,version=version+1 '
                                       'WHERE home_id=%s AND id=%s AND version=%s', (body.display_name, body.zone_id, agent_id, Jsonb(proposed['bindings']), home_id, controller_id, body.expected_version)).rowcount
                if not updated:
                    raise HTTPException(409, 'Controller changed. Reload before editing.')
                if changed:
                    for rule in rules:
                        conn.execute("UPDATE automations SET spec=%s,agent_id=%s,version=version+1,status='draft',requested=false,error=NULL WHERE home_id=%s AND id=%s",
                                     (Jsonb({**rule['spec'], **proposed['bindings']}), agent_id, home_id, rule['id']))
        else:
            controller_id = uid()
            db.execute('INSERT INTO semantic_controllers(id,home_id,display_name,domain,zone_id,agent_id,bindings) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                       (controller_id, home_id, body.display_name, domain, body.zone_id, agent_id, Jsonb(proposed['bindings'])))
        return view(db, get_controller(db, home_id, controller_id))


@non_watering_router.delete('/domains/{domain}/controllers/{controller_id}')
async def delete_controller(domain: str, controller_id: str, request: Request):
    db, home_id = request.app.state.db, request.state.home['id']
    async with request.app.state.automations._locks.setdefault('controllers:' + home_id, asyncio.Lock()):
        current = get_controller(db, home_id, controller_id)
        if current['domain'] != domain:
            raise HTTPException(404, 'Unknown controller')
        if db.one("SELECT id FROM automations WHERE home_id=%s AND controller_id=%s AND status<>'deleted'", (home_id, controller_id)):
            raise HTTPException(409, 'Remove the controller rules and wait for local confirmation first')
        with db.pool.connection() as conn:
            conn.execute('DELETE FROM automations WHERE home_id=%s AND controller_id=%s', (home_id, controller_id))
            conn.execute('DELETE FROM semantic_controllers WHERE home_id=%s AND id=%s', (home_id, controller_id))
        return {'deleted': True}
