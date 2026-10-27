"""Compile bounded automation building blocks to native, locally executed HA steps."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from backend.semantic.capabilities import infer_capabilities
from .program_models import AutomationProgram, ConditionGroup, TimeAnchor, input_ids


def template(expression):
    return '{{ ' + expression + ' }}'


def entity(record):
    from .compiler import _entity
    return _entity(record['entity_id'])


def fresh(record, seconds):
    q = entity(record)
    identity = f' and device_id({q}) == {json.dumps(record["attributes"]["device_id"])}' if record['attributes'].get('device_id') else ''
    return (f'(is_number(states({q})) and 0 <= as_timestamp(now()) - '
            f'as_timestamp(states[{q}].last_updated) < {seconds}{identity})')


def _condition_predicate(condition, records):
    if isinstance(condition, ConditionGroup):
        terms = [_condition_predicate(c, records) for c in condition.terms]
        if condition.kind == 'not':
            return f'(not {terms[0]})'
        return '(' + (' and ' if condition.kind == 'all' else ' or ').join(terms) + ')' if terms else 'true'
    if condition.kind=='trigger':
        return f"trigger.id | default('') == {json.dumps('event_'+condition.key)}"
    if condition.kind=='clock':
        clock="now().strftime('%H:%M')"
        a,b=json.dumps(condition.start),json.dumps(condition.end)
        return f'({a} <= {clock} < {b})' if condition.start<condition.end else f'({clock} >= {a} or {clock} < {b})'
    q = entity(records[condition.device_id])
    if condition.kind == 'state':
        return f'is_state({q}, {json.dumps(condition.state)})'
    op = {'above':'>', 'below':'<', 'at_least':'>=', 'at_most':'<='}[condition.comparison]
    return f'({fresh(records[condition.device_id], condition.max_age_seconds)} and states({q}) | float(0) {op} {condition.value})'


def condition_expression(condition, records):
    # Missing numeric data must not become true through a NOT/ANY group.
    guards=[]
    def collect(c):
        if isinstance(c,ConditionGroup):
            for child in c.terms:
                collect(child)
        elif c.kind=='number':
            guards.append(fresh(records[c.device_id],c.max_age_seconds))
    collect(condition)
    return '('+' and '.join([*guards,_condition_predicate(condition,records)])+')'


def anchor_trigger(anchor, identifier):
    if anchor.event == 'clock':
        return {'trigger':'time', 'at':anchor.at+':00', 'id':identifier}
    if anchor.event in {'sunrise','sunset'}:
        return {'trigger':'sun', 'event':anchor.event, 'offset':anchor.offset_minutes*60, 'id':identifier}
    if anchor.event == 'elevation':
        op = '>=' if anchor.direction == 'rising' else '<='
        rising = 'true' if anchor.direction == 'rising' else 'false'
        return {'trigger':'template', 'value_template':template(
            f"is_number(state_attr('sun.sun','elevation')) and state_attr('sun.sun','rising') == {rising} "
            f"and state_attr('sun.sun','elevation') | float(0) {op} {anchor.elevation}"), 'id':identifier}
    raise ValueError('An elapsed duration is relative to a running window')


def trigger_config(trigger, records, identifier):
    if trigger.kind == 'anchor':
        return anchor_trigger(trigger.anchor, identifier)
    record = records[trigger.device_id]
    if trigger.kind == 'state':
        return {'trigger':'state', 'entity_id':record['entity_id'], 'to':trigger.state, 'id':identifier}
    return {'trigger':'numeric_state', 'entity_id':record['entity_id'], trigger.comparison:trigger.value, 'id':identifier}


def all_steps(steps):
    for step in steps:
        if step.kind == 'choose':
            yield from all_steps(step.then)
            yield from all_steps(step.otherwise)
        else:
            yield step


def validate_capabilities(program, targets, inputs):
    control = program.control
    by_id = {d['id']:d for d in targets}
    from .program_models import SetStep
    steps=list(all_steps(control.steps)) if control.kind=='sequence' else [SetStep(brightness_pct=control.brightness_pct,kelvin=control.kelvin)] if control.kind=='switch' else []
    for step in steps:
        if step.kind != 'set':
            continue
        if not set(step.targets or []).issubset(by_id):
            raise HTTPException(422, 'An action target must be assigned to this controller')
        for device in [by_id[k] for k in step.targets] if step.targets else targets:
            caps = infer_capabilities(device['entity_id'],device['attributes'])
            if step.action not in caps.get('actions',[]):
                raise HTTPException(422, f'This target does not support {step.action}')
            if step.brightness_pct is not None and not caps.get('brightness'):
                raise HTTPException(422, 'Choose dimmable lamps for brightness actions')
            if step.kelvin is not None and not caps.get('color_temp'):
                raise HTTPException(422, 'Choose white-temperature lamps for Kelvin actions')
            if step.transition_seconds and (not device['entity_id'].startswith('light.') or not caps.get('transition')):
                raise HTTPException(422, 'This target does not support native fades; use a curve or remove the fade')
    if control.kind == 'band':
        sensors = [inputs[k] for k in control.sensor_ids]
        units = {d['attributes'].get('unit_of_measurement') for d in sensors}
        if len(units) != 1 or any(not d['entity_id'].startswith('sensor.') for d in sensors):
            raise HTTPException(422, 'Range control needs numeric sensors with matching units')
    for device in targets:
        caps = infer_capabilities(device['entity_id'],device['attributes'])
        if (program.end_action=='off' or program.condition_failure=='off') and 'turn_off' not in caps.get('actions',[]):
            raise HTTPException(422, 'This target cannot use a force-off policy; choose hold')
        if control.kind in {'band','switch'} and not {'turn_on','turn_off'}.issubset(caps.get('actions',[])):
            raise HTTPException(422, 'Range control needs switchable outputs')
        if control.kind == 'curve' and (not device['entity_id'].startswith('light.') or not caps.get('brightness')):
            raise HTTPException(422, 'A light-level curve needs dimmable lamps')
        if control.kind == 'curve' and control.color_mode == 'white' and not caps.get('color_temp'):
            raise HTTPException(422, 'Choose brightness only or lamps that support white temperature')


def identity_condition(device):
    if device['attributes'].get('device_id'):
        return [{'condition':'template','value_template':template(f'device_id({entity(device)}) == {json.dumps(device["attributes"]["device_id"])}')}]
    return []


def event(rule_id, device, desired, reason):
    return {'event':'semantic_home_action','event_data':{'automation_id':rule_id,'entity_id':device['entity_id'], 'desired':desired,'reason':reason}}


def set_actions(step, targets, rule_id, reason='program_action'):
    result = []
    for device in targets:
        if step.targets and device['id'] not in step.targets:
            continue
        caps = infer_capabilities(device['entity_id'], device['attributes'])
        data = {}
        if step.brightness_pct is not None:
            data['brightness_pct'] = step.brightness_pct
        if step.kelvin is not None:
            color = caps['color_temp']
            data['color_temp_kelvin'] = template(f"([[{step.kelvin}, state_attr({entity(device)},'min_color_temp_kelvin') | int({color['min_kelvin']})] | max, "
                                                 f"state_attr({entity(device)},'max_color_temp_kelvin') | int({color['max_kelvin']})] | min)")
        if step.transition_seconds:
            data['transition'] = step.transition_seconds
        action = {'action':device['entity_id'].split('.')[0]+'.'+step.action,
                  'target':{'entity_id':device['entity_id']}, 'continue_on_error':True}
        if data:
            action['data'] = data
        desired = {'turn_on':'on','turn_off':'off','start':'cleaning'}.get(step.action, step.action)
        guards = identity_condition(device)
        # Refuse a known-unavailable output instead of claiming success.
        guards.append({'condition':'template','value_template':template(f"states({entity(device)}) not in ['unknown','unavailable']")})
        result.append({'choose':[{'conditions':guards, 'sequence':[action,event(rule_id,device,desired,reason)]}]})
    return result


def compile_steps(steps, targets, records, rule_id):
    result = []
    for step in steps:
        if step.kind == 'set':
            result.extend(set_actions(step,targets,rule_id))
        elif step.kind == 'wait':
            result.append({'delay':{'seconds':step.seconds}})
        else:
            result.append({'choose':[{'conditions':[{'condition':'template','value_template':template(condition_expression(step.condition,records))}],
                                      'sequence':compile_steps(step.then,targets,records,rule_id)}],
                           'default':compile_steps(step.otherwise,targets,records,rule_id)})
    return result


def _curve_expression(control, field):
    points = sorted(control.points,key=lambda p:p.position)
    expression = str(getattr(points[-1],field))
    for a,b in reversed(list(zip(points,points[1:]))):
        segment = f'({getattr(a,field)} + ({getattr(b,field)} - {getattr(a,field)}) * (program_input - {a.position}) / ({b.position} - {a.position}))'
        expression = f'({segment} if program_input < {b.position} else {expression})'
    return f'({getattr(points[0],field)} if program_input <= {points[0].position} else {expression})'


def curve_body(program, targets, records, rule_id):
    control = program.control
    following = 'true'
    if control.driver == 'sun_elevation':
        driver = "state_attr('sun.sun','elevation') | float(0)"
        valid = "is_number(state_attr('sun.sun','elevation'))"
        if control.solar_direction!='any':
            rising='true' if control.solar_direction=='rising' else 'false'
            endpoint=max(p.position for p in control.points) if control.solar_direction=='rising' else min(p.position for p in control.points)
            op='>=' if control.solar_direction=='rising' else '<='
            following=f"(state_attr('sun.sun','rising') == {rising} or state_attr('sun.sun','elevation') | float(0) {op} {endpoint})"
    elif control.driver == 'measurement':
        record = records[control.sensor_id]
        driver, valid = f'states({entity(record)}) | float(0)', fresh(record,control.max_age_seconds)
    else:
        driver, valid = 'program_elapsed', 'true'
    gates = condition_expression(program.conditions,records)
    startup = f'[1, program_elapsed / {control.startup_minutes}] | min' if control.startup_minutes else '1'
    result = [{'variables':{'program_elapsed':template('[0,(as_timestamp(now()) - program_started) / 60] | max')}},
              {'variables':{'program_input':template(driver),'program_blend':template(startup)}},
              {'variables':{'program_brightness':template(f"({control.initial_brightness_pct} if program_first else ({control.initial_brightness_pct} + ({_curve_expression(control,'brightness_pct')} - {control.initial_brightness_pct}) * program_blend)) | round(0) | int"),
                            'program_kelvin':template(f"({control.initial_kelvin} if program_first else ({control.initial_kelvin} + ({_curve_expression(control,'kelvin')} - {control.initial_kelvin}) * program_blend)) | round(0) | int")}}]
    for device in targets:
        q = entity(device)
        caps = infer_capabilities(device['entity_id'], device['attributes'])
        initial = 'program_first'
        allowed = f"({initial} or is_state({q},'on'))" if control.respect_manual_off else f"states({q}) in ['on','off']"
        data = {'brightness_pct':template('program_brightness')}
        changed = f"((state_attr({q},'brightness') | float(0) / 255 * 100 - program_brightness) | abs >= 1)"
        if control.color_mode == 'white':
            color=caps['color_temp']
            clamped=f"([[program_kelvin, state_attr({q},'min_color_temp_kelvin') | int({color['min_kelvin']})] | max, state_attr({q},'max_color_temp_kelvin') | int({color['max_kelvin']})] | min)"
            data['color_temp_kelvin']=template(clamped)
            changed+=f" or ((state_attr({q},'color_temp_kelvin') | float(0) - {clamped}) | abs >= 25)"
        if caps.get('transition'):
            data['transition']=template('0 if program_first else 60')
        guards=identity_condition(device)+[{'condition':'template','value_template':template(
            f"({valid}) and ({gates}) and ({initial} or {following}) and {allowed} and ({initial} or {changed}) and states({q}) in ['on','off']")}]
        branches=[{'conditions':guards,'sequence':[{'action':'light.turn_on','target':{'entity_id':device['entity_id']},'data':data,'continue_on_error':True},event(rule_id,device,'on','program_curve')]}]
        if program.condition_failure == 'off':
            from .program_models import SetStep
            branches.insert(0,{'conditions':[{'condition':'template','value_template':template(f'not ({gates}) or not ({valid})')}],
                               'sequence':set_actions(SetStep(action='turn_off'),[device],rule_id,'condition_not_met')})
        result.append({'choose':branches})
    return result


def band_body(program, targets, records, rule_id, active='true'):
    c=program.control
    sensors=[records[k] for k in c.sensor_ids]
    valid='('+' and '.join(fresh(d,c.max_age_seconds) for d in sensors)+')'
    values='['+', '.join(f'states({entity(d)}) | float(0)' for d in sensors)+']'
    value=f'({values} | '+{'maximum':'max','minimum':'min','average':'average'}[c.aggregation]+')'
    gates=condition_expression(program.conditions,records)
    demand=f'{value} >= {c.upper}' if c.demand == 'above' else f'{value} <= {c.lower}'
    reached=f'{value} <= {c.lower}' if c.demand == 'above' else f'{value} >= {c.upper}'
    result=[]
    from .program_models import SetStep
    for device in targets:
        q=entity(device)
        elapsed=f'as_timestamp(now()) - as_timestamp(states[{q}].last_changed) >= {c.min_cycle_seconds}'
        force=f'not ({valid})'+(f' or not ({active})' if program.end_action == 'off' else '')+(f' or not ({gates})' if program.condition_failure == 'off' else '')
        off=f'({force}) or (({active}) and ({gates}) and ({valid}) and {reached} and ({elapsed}))'
        on=f'({active}) and ({gates}) and ({valid}) and {demand} and ({elapsed})'
        off_reason=template(f"'sensor_unavailable' if not ({valid}) else 'outside_window' if not ({active}) else 'condition_not_met' if not ({gates}) else '{'lower_threshold' if c.demand=='above' else 'upper_threshold'}'")
        result.append({'choose':[
            {'conditions':[{'condition':'state','entity_id':device['entity_id'],'state':'on'}, {'condition':'template','value_template':template(off)}],
             'sequence':set_actions(SetStep(action='turn_off'),[device],rule_id,off_reason)},
            {'conditions':[{'condition':'state','entity_id':device['entity_id'],'state':'off'}, {'condition':'template','value_template':template(on)}],
             'sequence':set_actions(SetStep(action='turn_on'),[device],rule_id,'upper_threshold' if c.demand=='above' else 'lower_threshold')},
        ]})
    return result


def _clock_active(timing, weekdays):
    # Reuse the established, DST-aware and overnight weekday semantics.
    from .compiler import _window
    from types import SimpleNamespace
    return _window(SimpleNamespace(weekdays=weekdays,start=timing.start.at,end=timing.end.at))


def switch_body(program,targets,records,rule_id,active='true'):
    from .program_models import SetStep
    gates=condition_expression(program.conditions,records)
    c=program.control
    result=[]
    for device in targets:
        q=entity(device)
        off=(f'not ({active})' if program.end_action=='off' else 'false')+(f' or not ({gates})' if program.condition_failure=='off' else '')
        changed=f"is_state({q},'off')"
        if c.brightness_pct is not None:
            changed+=f" or ((state_attr({q},'brightness') | float(0) / 255 * 100 - {c.brightness_pct}) | abs >= 1)"
        if c.kelvin is not None:
            changed+=f" or ((state_attr({q},'color_temp_kelvin') | float(0) - {c.kelvin}) | abs >= 25)"
        result.append({'choose':[
            {'conditions':[{'condition':'state','entity_id':device['entity_id'],'state':'on'}, {'condition':'template','value_template':template(off)}],
             'sequence':set_actions(SetStep(action='turn_off'),[device],rule_id,'outside_window')},
            {'conditions':[{'condition':'template','value_template':template(f'({active}) and ({gates}) and ({changed})')}],
             'sequence':set_actions(SetStep(brightness_pct=c.brightness_pct,kelvin=c.kelvin),[device],rule_id,'time_window')},
        ]})
    return result


def compile_program(config, rule_id, spec, targets, inputs):
    p=spec.program
    records={d['id']:d for d in targets+inputs}
    days=json.dumps(spec.weekdays)
    c=p.control
    config['mode']='queued'
    config['max_exceeded']='silent'
    if c.kind == 'sequence':
        config['triggers']=[trigger_config(t,records,'event_'+t.key) for t in p.timing.triggers]
        config['conditions']=[{'condition':'template','value_template':template(f'now().weekday() in {days}')}]
        from .program_models import SetStep
        config['actions']=[{'choose':[{'conditions':[{'condition':'template','value_template':template(condition_expression(p.conditions,records))}],
                                     'sequence':compile_steps(c.steps,targets,records,rule_id)}],
                           'default':set_actions(SetStep(action='turn_off'),targets,rule_id,'condition_not_met') if p.condition_failure=='off' else []}]
        return config
    clock_band=c.kind in {'band','switch'} and (p.timing.mode=='always' or (p.timing.start.event==p.timing.end.event=='clock'))
    if clock_band:
        config['triggers']=[{'trigger':'homeassistant','event':'start'}, {'trigger':'event','event_type':'automation_reloaded'},
                            {'trigger':'time_pattern','minutes':'/1'},
                            {'trigger':'state','entity_id':[d['entity_id'] for d in targets],'to':'on'}]
        if inputs:
            config['triggers'].append({'trigger':'state','entity_id':[d['entity_id'] for d in inputs]})
        active=f'now().weekday() in {days}'
        if p.timing.mode=='window':
            active=_clock_active(p.timing,spec.weekdays)
            config['triggers'] += [anchor_trigger(p.timing.start,'window_start'),anchor_trigger(p.timing.end,'window_end')]
        config['actions']=(band_body if c.kind=='band' else switch_body)(p,targets,records,rule_id,active)
        return config
    if c.kind=='curve' and p.timing.start.event==p.timing.end.event=='clock':
        start,end=p.timing.start.at,p.timing.end.at
        active=_clock_active(p.timing,spec.weekdays)
        initial="trigger.id | default('') == 'window_start'"
        previous_day=f"1 if now().strftime('%H:%M') < {json.dumps(end)} else 0" if start>end else '0'
        began=f"as_timestamp(today_at({json.dumps(start)}).replace(fold=now().fold) - timedelta(days=({previous_day})))"
        config['triggers']=[anchor_trigger(p.timing.start,'window_start'),anchor_trigger(p.timing.end,'window_end'),
                            {'trigger':'time_pattern','minutes':'/1'}, {'trigger':'homeassistant','event':'start'},
                            {'trigger':'event','event_type':'automation_reloaded'}]
        config['actions']=[{'variables':{'program_started':template(began),'program_first':template(initial)}}]
        branches=[]
        if p.end_action=='off':
            from .program_models import SetStep
            end_day="(now().weekday()-1)%7" if start>end else 'now().weekday()'
            branches.append({'conditions':[{'condition':'template','value_template':template(f"trigger.id | default('') == 'window_end' and {end_day} in {days}")}],
                             'sequence':set_actions(SetStep(action='turn_off'),targets,rule_id,'program_finished')})
        branches.append({'conditions':[{'condition':'template','value_template':template(active)}],
                         'sequence':curve_body(p,targets,records,rule_id)})
        config['actions'].append({'choose':branches})
        return config
    # Solar/event windows run as a local HA loop. No daily backend scheduling,
    # extra helpers, or stored synthetic measurements are required.
    timing=p.timing
    config['mode']='single'
    config['triggers']=[anchor_trigger(timing.start,'window_start')]
    config['conditions']=[{'condition':'template','value_template':template(f'now().weekday() in {days}')}]
    stop='program_started + '+str(p.max_run_minutes*60)
    if timing.end.event=='elapsed':
        stop='program_started + '+str(min(timing.end.minutes,p.max_run_minutes)*60)
    elif timing.end.event=='clock':
        clock=json.dumps(timing.end.at)
        stop=f"[program_started + {p.max_run_minutes*60}, as_timestamp(today_at({clock}) + timedelta(days=1 if as_timestamp(today_at({clock})) <= program_started else 0))] | min"
    config['actions']=[{'variables':{'program_started':template('as_timestamp(now())'),'program_first':True}},
                       {'variables':{'program_deadline':template(stop)}}]
    body=curve_body(p,targets,records,rule_id) if c.kind=='curve' else (band_body if c.kind=='band' else switch_body)(p,targets,records,rule_id)
    wait={'timeout':template('[60,[0,program_deadline-as_timestamp(now())] | max] | min'),'continue_on_timeout':True}
    if timing.end.event not in {'elapsed'}:
        wait['wait_for_trigger']=[anchor_trigger(timing.end,'window_end')]
    else:
        # wait_template:false supplies the same wait.completed/trigger result.
        wait['wait_template']='{{ false }}'
    end_passed='false'
    if timing.end.event=='elevation':
        op='<=' if timing.end.direction=='setting' else '>='
        rising='false' if timing.end.direction=='setting' else 'true'
        end_passed=f"is_number(state_attr('sun.sun','elevation')) and state_attr('sun.sun','rising') == {rising} and state_attr('sun.sun','elevation') | float(0) {op} {timing.end.elevation}"
    config['actions'].append({'repeat':{'while':[{'condition':'template','value_template':template(
        f'as_timestamp(now()) < program_deadline and not ({end_passed}) and (wait is not defined or wait.trigger | default(none) is none)')}],
        'sequence':body+[{'variables':{'program_first':False}},wait]}})
    if p.end_action=='off':
        from .program_models import SetStep
        config['actions'].extend(set_actions(SetStep(action='turn_off'),targets,rule_id,'program_finished'))
    return config
