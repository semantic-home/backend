"""Read-only evaluation of the same validated automation building blocks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import elevation

from .measurements import measurement_status
from .models import HomeLocation
from .program_models import ConditionGroup
from .solar import event_time
from .lighting import phase_crossing


def anchor_time(anchor, day, zone, location, start=None):
    if anchor.event=='elapsed':
        return (start.astimezone(timezone.utc)+timedelta(minutes=anchor.minutes)).astimezone(zone) if start else None
    if anchor.event=='clock':
        return event_time('time',anchor.at,0,day,zone,location)
    if not location:
        return None
    if anchor.event=='elevation':
        observer=Observer(location.latitude,location.longitude,location.elevation)
        return phase_crossing(observer,anchor.elevation,day,zone,'dawn' if anchor.direction=='rising' else 'dusk')
    return event_time(anchor.event,None,anchor.offset_minutes,day,zone,location)


def window_bounds(program, day, zone, location):
    if program.timing.mode!='window':
        return None,None
    start=anchor_time(program.timing.start,day,zone,location)
    stop=anchor_time(program.timing.end,day,zone,location,start)
    if start and stop and stop<=start and program.timing.end.event!='elapsed':
        stop=anchor_time(program.timing.end,day+timedelta(days=1),zone,location,start)
    if start and stop is None:
        stop=(start.astimezone(timezone.utc)+timedelta(minutes=program.max_run_minutes)).astimezone(zone)
    if start and stop and not (program.timing.start.event==program.timing.end.event=='clock'):
        deadline=start.astimezone(timezone.utc)+timedelta(minutes=program.max_run_minutes)
        if stop.astimezone(timezone.utc)>deadline:
            stop=deadline.astimezone(zone)
    return start,stop


def conditions_met(condition,records,now):
    invalid=[]
    def check(c):
        if isinstance(c,ConditionGroup):
            for child in c.terms:
                check(child)
        elif c.kind=='number' and not measurement_status(records[c.device_id],c.max_age_seconds,now)['valid']:
            invalid.append(c.device_id)
    check(condition)
    return not invalid and _conditions_met(condition,records,now)


def _conditions_met(condition,records,now):
    if isinstance(condition,ConditionGroup):
        values=[_conditions_met(c,records,now) for c in condition.terms]
        if condition.kind=='not':
            return not values[0]
        return all(values) if condition.kind=='all' else any(values)
    if condition.kind=='trigger':
        return False  # No event is being fired by a read-only preview.
    if condition.kind=='clock':
        clock=now.strftime('%H:%M')
        return condition.start<=clock<condition.end if condition.start<condition.end else clock>=condition.start or clock<condition.end
    device=records[condition.device_id]
    if condition.kind=='state':
        return device['state']==condition.state
    measurement=measurement_status(device,condition.max_age_seconds,now)
    if not measurement['valid']:
        return False
    value=measurement['value']
    return {'above':value>condition.value,'below':value<condition.value,
            'at_least':value>=condition.value,'at_most':value<=condition.value}[condition.comparison]


def curve_value(control,value,elapsed):
    points=sorted(control.points,key=lambda p:p.position)
    a=b=points[0] if value<=points[0].position else points[-1]
    for left,right in zip(points,points[1:]):
        if left.position<=value<=right.position:
            a,b=left,right
            break
    ratio=0 if a.position==b.position else (value-a.position)/(b.position-a.position)
    blend=min(1,max(0,elapsed/control.startup_minutes)) if control.startup_minutes else 1
    return {field:round(initial+(getattr(a,field)+(getattr(b,field)-getattr(a,field))*ratio-initial)*blend)
            for field,initial in [('brightness_pct',control.initial_brightness_pct),('kelvin',control.initial_kelvin)]}


def preview_program(spec,targets,inputs,metadata,home_timezone,connection_name,connected,now):
    p,c=spec.program,spec.program.control
    zone=ZoneInfo(home_timezone)
    local=now.astimezone(zone)
    location=HomeLocation.model_validate(metadata['location']) if metadata.get('location') else None
    records={d['id']:d for d in targets+inputs}
    from .measurements import measurement_limits
    limits=measurement_limits(spec.model_dump())
    measurements=[{'id':key,'name':records[key]['attributes'].get('friendly_name',records[key]['entity_id']),
                   **measurement_status(records[key],seconds,now)} for key,seconds in limits.items()]
    result={'version':2,'timezone':home_timezone,'now':local.isoformat(), 'location_name':location.name if location else None,
            'connection_name':connection_name,'connected':connected,'conditions_met':conditions_met(p.conditions,records,local),
            'measurements':measurements,'events':[],'warnings':[],'timeline':None,'items':[]}
    if not connected:
        result['warnings'].append('The local agent is offline; the preview uses its last reported states and location.')
    if metadata.get('timezone')!=home_timezone:
        result['warnings'].append('Home Assistant and household time zones must match before activation.')
    if p.timing.mode=='events':
        for delta in range(-1,9):
            day=local.date()+timedelta(days=delta)
            for trigger in p.timing.triggers:
                if trigger.kind=='anchor':
                    at=anchor_time(trigger.anchor,day,zone,location)
                    if at and at.weekday() in spec.weekdays and at.astimezone(timezone.utc)>now:
                        result['events'].append({'at':at.isoformat(),'event':trigger.anchor.event})
        result['events']=sorted(result['events'],key=lambda e:datetime.fromisoformat(e['at']).timestamp())[:8]
        result['warnings'].append('Event rules run when an event occurs. State/threshold events cannot be predicted from the current value; missed events are not replayed after restart.')
    start=stop=None
    active=local.weekday() in spec.weekdays
    if p.timing.mode=='window':
        start,stop=window_bounds(p,local.date(),zone,location)
        active=False
        for delta in (-1,0):
            day=local.date()+timedelta(days=delta)
            a,b=window_bounds(p,day,zone,location)
            if a and b and a.weekday() in spec.weekdays and a.astimezone(timezone.utc)<=now<b.astimezone(timezone.utc):
                active=True
        result['events']=[{'at':at.isoformat(),'event':name} for name,at in [('start',start),('end',stop)]] if start and stop else []
        natural_end=anchor_time(p.timing.end,local.date(),zone,location,start)
        if start and natural_end and natural_end<=start and p.timing.end.event!='elapsed':
            natural_end=anchor_time(p.timing.end,local.date()+timedelta(days=1),zone,location,start)
        if stop and natural_end and stop.astimezone(timezone.utc)<natural_end.astimezone(timezone.utc):
            result['warnings'].append(f'The {p.max_run_minutes}-minute running limit ends this window before the selected end event. Change the limit in Advanced to reach that event.')
        if not start or not natural_end:
            result['warnings'].append('A selected solar event does not occur today, or its home location is missing. No substitute clock time is invented.')
        if not (p.timing.start.event==p.timing.end.event=='clock'):
            result['warnings'].append('This event window starts locally at its start event. After an HA restart/reload it waits for the next start; a missed start is not replayed. Manual-off lamps remain off unless enforcement is selected.')
            result['warnings'].append(f'Control stops after at most {p.max_run_minutes} minutes if its end event is missing. Ending behaviour: {p.end_action}.')
    if c.kind=='curve' and start and stop:
        observer=Observer(location.latitude,location.longitude,location.elevation) if location else None
        samples=[]
        step=max(60,int((stop-start).total_seconds()/180))
        count=int((stop.astimezone(timezone.utc)-start.astimezone(timezone.utc)).total_seconds())
        offsets=sorted({0,count,*range(0,count+1,step),min(count,c.startup_minutes*60)})
        measurement=measurement_status(records[c.sensor_id],c.max_age_seconds,now) if c.driver=='measurement' else None
        valid_driver=observer is not None if c.driver=='sun_elevation' else measurement['valid'] if measurement else True
        if valid_driver:
            previous=None
            for offset in offsets:
                at=(start.astimezone(timezone.utc)+timedelta(seconds=offset)).astimezone(zone)
                value=elevation(observer,at) if c.driver=='sun_elevation' else measurement['value'] if measurement else offset/60
                levels=curve_value(c,value,offset/60)
                if c.driver=='sun_elevation' and c.solar_direction!='any':
                    rising=elevation(observer,at+timedelta(seconds=30))>=value
                    endpoint=max(pt.position for pt in c.points) if c.solar_direction=='rising' else min(pt.position for pt in c.points)
                    following=(rising or value>=endpoint) if c.solar_direction=='rising' else (not rising or value<=endpoint)
                    if not following and previous is not None:
                        levels=previous
                if offset==0:
                    levels={'brightness_pct':c.initial_brightness_pct,'kelvin':c.initial_kelvin}
                previous=levels
                samples.append({'at':at.isoformat(),'input':round(value,3),**levels})
            if p.end_action=='off':
                samples.append({**samples[-1],'brightness_pct':0})
        else:
            result['warnings'].append('The curve input is unavailable; no light-level curve can be calculated.')
        if measurement:
            result['warnings'].append('A measurement-driven curve shows the current reading held constant; future measurements cannot be predicted.')
        result['timeline']={'start':start.isoformat(),'stop':stop.isoformat(),'samples':samples,
                            'available':valid_driver,'driver':c.driver}
    band_value=None
    if c.kind=='band':
        values=[measurement_status(records[k],c.max_age_seconds,now) for k in c.sensor_ids]
        if all(v['valid'] for v in values):
            numbers=[v['value'] for v in values]
            band_value=max(numbers) if c.aggregation=='maximum' else min(numbers) if c.aggregation=='minimum' else sum(numbers)/len(numbers)
        result['aggregate']=band_value
    for target in targets:
        desired,reason='scheduled','program_event'
        if c.kind=='band':
            desired=target['state']
            if band_value is None:
                desired,reason='off','sensor_unavailable'
            elif not active:
                desired,reason=('off' if p.end_action=='off' or (not result['conditions_met'] and p.condition_failure=='off') else target['state']),'outside_window'
            elif not result['conditions_met']:
                desired,reason=('off' if p.condition_failure=='off' else target['state']),'condition_not_met'
            elif band_value>=c.upper:
                desired,reason=('on' if c.demand=='above' else 'off'),'upper_threshold'
            elif band_value<=c.lower:
                desired,reason=('off' if c.demand=='above' else 'on'),'lower_threshold'
            else:
                reason='keep_state'
        if c.kind=='switch':
            force_off=(not active and p.end_action=='off') or (not result['conditions_met'] and p.condition_failure=='off')
            if force_off:
                desired,reason='off','outside_window' if not active else 'condition_not_met'
            elif active and result['conditions_met']:
                desired,reason='on','time_window'
            else:
                desired,reason=target['state'],'keep_state'
        result['items'].append({'id':target['id'],'name':target['attributes'].get('friendly_name',target['entity_id']),
                                'current':target['state'],'desired':desired,'reason':reason})
    return result
