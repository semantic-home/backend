"""Consistent measurement age/validity for controller cards and effect previews."""
from datetime import datetime, timezone
import math


def measurement_status(device, max_age_seconds=1800, now=None):
    now=now or datetime.now(timezone.utc)
    raw=device['attributes'].get('_ha_last_updated') or device.get('seen_at')
    age=None
    issue=None
    try:
        updated=datetime.fromisoformat(raw) if isinstance(raw,str) else raw
        age=(now-updated).total_seconds()
        if not 0 <= age < max_age_seconds:
            issue='stale' if age>=max_age_seconds else 'invalid_timestamp'
    except (TypeError,ValueError):
        issue='invalid_timestamp'
    try:
        value=float(device['state'])
        if not math.isfinite(value):
            raise ValueError('non-finite')
    except (TypeError,ValueError):
        value=None
        issue='invalid_value'
    if not device.get('available',False) or device['state'] in {'unknown','unavailable'}:
        issue='unavailable'
    return {'value':value,'last_reported_at':raw.isoformat() if hasattr(raw,'isoformat') else raw,
            'age_seconds':round(age,1) if age is not None else None,'max_age_seconds':max_age_seconds,
            'valid':issue is None,'issue':issue}


def measurement_limits(spec):
    limits={}
    if spec.get('kind')=='humidity_band':
        for key in spec.get('sensor_ids') or ([spec['sensor_id']] if spec.get('sensor_id') else []):
            limits[key]=spec.get('sensor_stale_seconds',1800)
    def walk(value):
        if isinstance(value,dict):
            if value.get('kind')=='band':
                for key in value['sensor_ids']:
                    limits[key]=min(limits.get(key,86400),value.get('max_age_seconds',1800))
            if value.get('kind')=='number':
                key=value['device_id']
                limits[key]=min(limits.get(key,86400),value.get('max_age_seconds',1800))
            if value.get('driver')=='measurement' and value.get('sensor_id'):
                key=value['sensor_id']
                limits[key]=min(limits.get(key,86400),value.get('max_age_seconds',1800))
            for child in value.values():
                if isinstance(child,(dict,list)):
                    walk(child)
        elif isinstance(value,list):
            for child in value:
                walk(child)
    if spec.get('program'):
        walk(spec['program'])
    return limits
