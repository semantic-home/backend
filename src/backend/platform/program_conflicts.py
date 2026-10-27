"""Prove timed ownership is separate; unpredictable event rules remain exclusive."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import HomeLocation
from .program_preview import anchor_time, window_bounds


def _duration(steps):
    return sum(s.seconds if s.kind=='wait' else max(_duration(s.then),_duration(s.otherwise)) if s.kind=='choose' else s.transition_seconds for s in steps)


def intervals(spec,metadata,home_timezone,now):
    zone=ZoneInfo(home_timezone)
    location=HomeLocation.model_validate(metadata['location']) if metadata.get('location') else None
    if spec.kind not in {'program','lighting_ramp'}:
        return None
    p=spec.program
    if p and p.timing.mode=='always':
        return None
    if p and p.timing.mode=='events' and any(t.kind!='anchor' for t in p.timing.triggers):
        return None
    result=[]
    for delta in range(-1,371):
        day=now.astimezone(zone).date()+timedelta(days=delta)
        windows=[]
        if spec.kind=='lighting_ramp':
            windows=[(datetime.combine(day,time.fromisoformat(spec.ramp.start_at),zone),
                      datetime.combine(day,time.fromisoformat(spec.ramp.stop_at),zone))]
        elif p.timing.mode=='window':
            a,b=window_bounds(p,day,zone,location)
            if a and b is None:
                b=(a.astimezone(timezone.utc)+timedelta(minutes=p.max_run_minutes)).astimezone(zone)
            windows=[(a,b)]
        else:
            duration=max(1,_duration(p.control.steps))
            for trigger in p.timing.triggers:
                a=anchor_time(trigger.anchor,day,zone,location)
                windows.append((a,a+timedelta(seconds=duration) if a else None))
        for a,b in windows:
            if a and b and a.weekday() in spec.weekdays:
                result.append((a.astimezone(timezone.utc),b.astimezone(timezone.utc)))
            elif a is None and location is None:
                return None
    return sorted(result)


def separated(spec,other,metadata,home_timezone,now):
    first=intervals(spec,metadata,home_timezone,now)
    second=intervals(other,metadata,home_timezone,now)
    if first is None or second is None:
        return False
    i=j=0
    while i<len(first) and j<len(second):
        a,b=first[i],second[j]
        if a[1]<b[0]:
            i+=1
        elif b[1]<a[0]:
            j+=1
        else:
            return False
    return True
