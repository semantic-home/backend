from __future__ import annotations

import copy
from datetime import datetime, timedelta
import math
from statistics import mean
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from jinja2 import Environment
from jinja2.nativetypes import NativeEnvironment

from backend.platform.compiler import compile_automation
from backend.platform.measurements import measurement_status
from backend.platform.models import AutomationSpec
from backend.platform.program import condition_expression,validate_capabilities
from backend.platform.program_conflicts import separated
from backend.platform.program_models import AutomationProgram,ConditionGroup,input_ids
from backend.platform.program_preview import conditions_met,preview_program

RULE='01234567-0123-4123-8123-0123456789ab'
NOW=datetime.fromisoformat('2026-10-27T12:00:00+01:00')
ATTRS={'supported_color_modes':['color_temp'],'min_color_temp_kelvin':2702,'max_color_temp_kelvin':6666,
       'supported_features':44,'device_id':'physical','registry_id':'registry'}
TARGET={'id':'lamp','entity_id':'light.room','state':'on','available':True,'attributes':ATTRS,'seen_at':NOW}
METADATA={'timezone':'Europe/Berlin','location':{'name':'Meckenheim','latitude':50.625,'longitude':7.022,'elevation':180}}


def sensor(key,value,age=0,unit='%',available=True):
    return {'id':key,'entity_id':'sensor.'+key,'state':str(value),'available':available,'seen_at':NOW,
            'attributes':{'unit_of_measurement':unit,'_ha_last_updated':(NOW-timedelta(seconds=age)).isoformat()}}


def band(**patch):
    data={'version':2,'timing':{'mode':'window','start':{'event':'clock','at':'06:00'},'end':{'event':'clock','at':'22:00'}},
          'control':{'kind':'band','sensor_ids':['kitchen','living'],'lower':52,'upper':55},'end_action':'off','condition_failure':'off'}
    data.update(patch)
    return AutomationProgram.model_validate(data)


def dusk():
    return AutomationProgram.model_validate({'version':2,'timing':{'mode':'window','start':{'event':'sunset'},
        'end':{'event':'elevation','elevation':-20,'direction':'setting'}},'max_run_minutes':360,
        'control':{'kind':'curve','driver':'sun_elevation','solar_direction':'setting','initial_brightness_pct':100,'initial_kelvin':5500,'startup_minutes':1,
            'points':[{'position':x,'brightness_pct':v,'kelvin':k} for x,v,k in [(0,100,5500),(-6,45,4000),(-12,10,3000),(-18,5,2702),(-20,1,2702)]]}})


def spec(program,**patch):
    return AutomationSpec(name='Reusable control',kind='program',targets=['lamp'],program=program,**patch)


class ProgramTest(unittest.TestCase):
    def context(self,inputs=(),state='on',age=300,now=NOW,angle=-9,rising=False):
        entries={d['entity_id']:SimpleNamespace(state=d['state'],last_updated=datetime.fromisoformat(d['attributes']['_ha_last_updated'])) for d in inputs}
        entries[TARGET['entity_id']]=SimpleNamespace(state=state,last_changed=now-timedelta(seconds=age))
        class States:
            def __call__(self,eid):return entries[eid].state
            def __getitem__(self,eid):return entries[eid]
        attributes={**ATTRS,'brightness':255,'color_temp_kelvin':5500}
        return {'states':States(),'now':lambda:now,'as_timestamp':lambda x:x.timestamp(),'as_datetime':lambda x:datetime.fromtimestamp(x,now.tzinfo),
                'today_at':lambda x:now.replace(hour=int(x[:2]),minute=int(x[3:]),second=0,microsecond=0),
                'is_number':lambda x: isinstance(x,(str,int,float)) and x not in {'unknown','unavailable'} and math.isfinite(float(x)),
                'is_state':lambda eid,value:entries[eid].state==value,'device_id':lambda eid:'physical',
                'state_attr':lambda eid,key:({'elevation':angle,'rising':rising} if eid=='sun.sun' else attributes).get(key),
                'trigger':SimpleNamespace(id='window_start'),'timedelta':timedelta}

    def render(self,text,context):
        env=NativeEnvironment()
        env.filters['average']=mean
        return env.from_string(text).render(**context)

    def test_band_uses_both_sensors_and_retains_hysteresis(self):
        for readings,expected in [((55,50.6),[False,True]),((54,51),[False,False]),((52,51),[True,False]),((51,56),[False,True])]:
            inputs=[sensor(k,v) for k,v in zip(['kitchen','living'],readings)]
            config=compile_automation(RULE,spec(band()),[TARGET],inputs,1)
            context=self.context(inputs)
            actual=[self.render(b['conditions'][1]['value_template'],context) for b in config['actions'][0]['choose']]
            self.assertEqual(actual,expected)

    def test_stale_input_still_forces_off_and_overrides_minimum_cycle(self):
        inputs=[sensor('kitchen',57),sensor('living',50.6,age=1801)]
        config=compile_automation(RULE,spec(band()),[TARGET],inputs,1)
        context=self.context(inputs,age=1)
        branches=config['actions'][0]['choose']
        self.assertTrue(self.render(branches[0]['conditions'][1]['value_template'],context))
        self.assertFalse(self.render(branches[1]['conditions'][1]['value_template'],context))
        reason=branches[0]['sequence'][0]['choose'][0]['sequence'][-1]['event_data']['reason']
        self.assertEqual(self.render(reason,context),'sensor_unavailable')

    def test_schedule_cutoff_is_independent_of_readings_and_cycle(self):
        now=NOW.replace(hour=22)
        inputs=[sensor('kitchen',60),sensor('living',59)]
        config=compile_automation(RULE,spec(band()),[TARGET],inputs,1)
        context=self.context(inputs,age=0,now=now)
        self.assertEqual([self.render(b['conditions'][1]['value_template'],context) for b in config['actions'][0]['choose']],[True,False])
        self.assertIn({'trigger':'time','at':'22:00:00','id':'window_end'},config['triggers'])

    def test_minimum_average_and_low_demand_work_without_new_rule_kinds(self):
        inputs=[sensor('kitchen',10,unit='°C'),sensor('living',30,unit='°C')]
        for aggregation,demand,lower,upper,expected in [('average','below',21,25,True),('minimum','above',5,9,True),('maximum','below',20,25,False)]:
            program=band();program.control.aggregation=aggregation;program.control.demand=demand;program.control.lower=lower;program.control.upper=upper
            validate_capabilities(program,[TARGET],{d['id']:d for d in inputs})
            config=compile_automation(RULE,spec(program),[TARGET],inputs,1)
            self.assertEqual(self.render(config['actions'][0]['choose'][1]['conditions'][1]['value_template'],self.context(inputs)),expected)

    def test_not_and_any_never_turn_stale_numeric_conditions_true(self):
        inputs=[sensor('living',50,age=1801)]
        for kind in ['not','any','all']:
            group=ConditionGroup.model_validate({'kind':kind,'terms':[{'kind':'number','device_id':'living','comparison':'above','value':55}]})
            expression=condition_expression(group,{'living':inputs[0]})
            self.assertFalse(self.render('{{ '+expression+' }}',self.context(inputs)))
            self.assertFalse(conditions_met(group,{'living':inputs[0]},NOW))

    def test_dusk_has_a_real_sunset_start_and_an_end_past_astronomical_twilight(self):
        config=compile_automation(RULE,spec(dusk()),[TARGET],[],1)
        self.assertEqual(config['triggers'],[{'trigger':'sun','event':'sunset','offset':0,'id':'window_start'}])
        loop=config['actions'][2]['repeat']
        self.assertIn('-20.0',loop['while'][0]['value_template'])
        self.assertEqual(loop['sequence'][-1]['wait_for_trigger'][0]['trigger'],'template')
        result=preview_program(spec(dusk()),[TARGET],[],METADATA,'Europe/Berlin','HA',True,NOW)
        self.assertTrue(result['timeline']['available'])
        self.assertTrue(result['events'][0]['at'].startswith('2026-10-27T17:'))
        last=result['timeline']['samples'][-1]
        self.assertAlmostEqual(last['input'],-20,delta=.01)
        self.assertEqual(last['brightness_pct'],1)
        self.assertLess(last['at'],'2026-10-27T20:00:00+01:00')

    def test_clock_curve_start_and_restart_semantics_preserve_existing_dawn(self):
        p=dusk();p.timing.start.event='clock';p.timing.start.at='06:00';p.timing.end.event='clock';p.timing.end.at='12:00';p.timing.end.elevation=None
        p.control.solar_direction='rising';p.control.initial_brightness_pct=1;p.control.initial_kelvin=2702;p.control.startup_minutes=10
        config=compile_automation(RULE,spec(p),[TARGET],[],1)
        self.assertTrue(any(t['trigger']=='homeassistant' for t in config['triggers']))
        context=self.context(now=NOW.replace(hour=6,minute=0),angle=10,rising=True,state='off')
        for action in config['actions'][:1]:
            for key,value in action['variables'].items():context[key]=self.render(value,context)
        body=config['actions'][1]['choose'][-1]['sequence']
        for action in body:
            for key,value in action.get('variables',{}).items():context[key]=self.render(value,context)
        self.assertEqual(context['program_brightness'],1)
        self.assertEqual(context['program_kelvin'],2702)
        context['program_first']=False
        guard=body[-1]['choose'][-1]['conditions'][-1]['value_template']
        self.assertFalse(self.render(guard,context))

    def test_solar_curve_holds_during_the_opposite_phase(self):
        p=dusk()
        config=compile_automation(RULE,spec(p),[TARGET],[],1)
        body=config['actions'][2]['repeat']['sequence']
        context=self.context(angle=-12,rising=True)
        context.update(program_started=NOW.timestamp()-3600,program_first=False)
        for action in body[:3]:
            for key,value in action.get('variables',{}).items():context[key]=self.render(value,context)
        guard=body[3]['choose'][-1]['conditions'][-1]['value_template']
        self.assertFalse(self.render(guard,context))

    def test_elapsed_and_measurement_curves_use_the_same_control(self):
        for driver,inputs in [('elapsed_minutes',[]),('measurement',[sensor('living',50)])]:
            p=dusk();p.control.driver=driver;p.control.sensor_id='living' if inputs else None;p.control.solar_direction='any';p.control.startup_minutes=0
            p.control.points=[type(p.control.points[0])(position=0,brightness_pct=10,kelvin=2702),type(p.control.points[0])(position=100,brightness_pct=90,kelvin=5500)]
            config=compile_automation(RULE,spec(p),[TARGET],inputs,1)
            body=config['actions'][2]['repeat']['sequence']
            context=self.context(inputs);context.update(program_started=NOW.timestamp()-3000,program_first=False)
            for action in body[:3]:
                for key,value in action.get('variables',{}).items():context[key]=self.render(value,context)
            self.assertEqual(context['program_brightness'],50)

    def test_sequences_support_stable_event_keys_and_nested_choices(self):
        p=AutomationProgram.model_validate({'version':2,'timing':{'mode':'events','triggers':[{'kind':'anchor','key':'on','anchor':{'event':'sunset'}},{'kind':'state','key':'motion','device_id':'motion','state':'on'}]},
            'control':{'kind':'sequence','steps':[{'kind':'choose','condition':{'kind':'all','terms':[{'kind':'trigger','key':'on'}]},
                'then':[{'kind':'set','action':'turn_on','brightness_pct':25},{'kind':'wait','seconds':30},{'kind':'set','action':'turn_off'}],
                'otherwise':[{'kind':'set','action':'turn_on','brightness_pct':50}]}]}})
        self.assertEqual(input_ids(p),['motion'])
        config=compile_automation(RULE,spec(p),[TARGET],[sensor('motion','on')],1)
        self.assertEqual([t['id'] for t in config['triggers']],['event_on','event_motion'])
        nested=config['actions'][0]['choose'][0]['sequence'][0]['choose'][0]
        context=self.context();context['trigger']=SimpleNamespace(id='event_on')
        self.assertTrue(self.render(nested['conditions'][0]['value_template'],context))
        self.assertEqual(nested['sequence'][1],{'delay':{'seconds':30}})

    def test_action_targets_cannot_escape_the_controller_and_unsupported_fades_are_rejected(self):
        def sequence(step):
            return AutomationProgram.model_validate({'timing':{'mode':'events','triggers':[{'kind':'anchor','anchor':{'event':'clock','at':'08:00'}}]},'control':{'kind':'sequence','steps':[step]}})
        with self.assertRaises(HTTPException):validate_capabilities(sequence({'kind':'set','targets':['foreign']}),[TARGET],{})
        with self.assertRaises(ValueError):sequence({'kind':'set','targets':[]})
        with self.assertRaises(ValueError):sequence({'kind':'set','action':'shell_command.run'})
        with self.assertRaises(ValueError):sequence({'kind':'set','action':'turn_off','brightness_pct':20})
        plain={**TARGET,'attributes':{'supported_color_modes':['brightness']}}
        with self.assertRaises(HTTPException):validate_capabilities(sequence({'kind':'set','transition_seconds':20}),[plain],{})
        with self.assertRaises(HTTPException):validate_capabilities(sequence({'kind':'set','kelvin':3000}),[plain],{})

    def test_model_rejects_ambiguous_timing_unbounded_waits_and_unknown_event_references(self):
        raw=dusk().model_dump()
        for patch in [{'version':3},{'control':{**raw['control'],'points':[raw['control']['points'][0]]}},
                      {'timing':{'mode':'window','start':{'event':'elapsed','minutes':5},'end':{'event':'clock','at':'12:00'}}},
                      {'conditions':{'kind':'all','terms':[{'kind':'trigger','key':'missing'}]}},
                      {'control':{**raw['control'],'points':[{**raw['control']['points'][0],'position':float('nan')},raw['control']['points'][1]]}}]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):AutomationProgram.model_validate({**raw,**patch})
        with self.assertRaises(ValueError):
            AutomationProgram.model_validate({'timing':{'mode':'events','triggers':[{'kind':'anchor','anchor':{'event':'clock','at':'08:00'}}]},'max_run_minutes':1,'control':{'kind':'sequence','steps':[{'kind':'wait','seconds':61}]}})

    def test_all_generated_templates_parse_for_each_control_and_timing_mode(self):
        programs=[dusk(),band(),band(timing={'mode':'always'}),band(timing=dusk().timing.model_dump()),
                  AutomationProgram.model_validate({'timing':{'mode':'always'},'control':{'kind':'switch'}})]
        elapsed=dusk();elapsed.timing.end=type(elapsed.timing.end)(event='elapsed',minutes=30);programs.append(elapsed)
        for p in programs:
            inputs=[sensor('kitchen',55),sensor('living',51)] if p.control.kind=='band' else []
            config=compile_automation(RULE,spec(p),[TARGET],inputs,1)
            def walk(value):
                if isinstance(value,str) and '{{' in value:Environment().parse(value)
                elif isinstance(value,dict):
                    for child in value.values():walk(child)
                elif isinstance(value,list):
                    for child in value:walk(child)
            walk(config)

    def test_freshness_metadata_exposes_stale_unavailable_invalid_and_future_values(self):
        for device,issue in [(sensor('a',50,age=1801),'stale'),(sensor('a','nan'),'invalid_value'),(sensor('a',50,available=False),'unavailable'),(sensor('a',50,age=-1),'invalid_timestamp')]:
            status=measurement_status(device,1800,NOW)
            self.assertFalse(status['valid']);self.assertEqual(status['issue'],issue)
        self.assertTrue(measurement_status(sensor('a',50),1800,NOW)['valid'])

    def test_preview_band_matches_required_multi_sensor_behavior_and_identifies_the_stale_sensor(self):
        for values,expected in [([sensor('kitchen',55),sensor('living',50.6)],'on'),([sensor('kitchen',51),sensor('living',50.6)],'off'),([sensor('kitchen',57),sensor('living',50.6,age=1801)],'off')]:
            result=preview_program(spec(band()),[TARGET],values,METADATA,'Europe/Berlin','HA',True,NOW)
            self.assertEqual(result['items'][0]['desired'],expected)
            if not result['measurements'][1]['valid']:self.assertEqual(result['measurements'][1]['issue'],'stale')

    def test_switch_preview_applies_the_correct_end_and_condition_policy(self):
        for hour,matched,end,failure,expected in [(12,False,'off','hold','on'),(23,True,'hold','off','on'),(12,False,'hold','off','off'),(23,True,'off','hold','off')]:
            p=AutomationProgram.model_validate({'timing':{'mode':'window','start':{'event':'clock','at':'06:00'},'end':{'event':'clock','at':'22:00'}},'control':{'kind':'switch'},'end_action':end,'condition_failure':failure,
                'conditions':{'kind':'all','terms':[] if matched else [{'kind':'state','device_id':'lamp','state':'off'}]}})
            result=preview_program(spec(p),[TARGET],[],METADATA,'Europe/Berlin','HA',True,NOW.replace(hour=hour))
            self.assertEqual(result['items'][0]['desired'],expected)

    def test_clock_curve_queues_its_end_edge_while_solar_loop_stays_single(self):
        p=dusk();p.timing.start=type(p.timing.start)(event='clock',at='06:00');p.timing.end=type(p.timing.end)(event='clock',at='12:00')
        self.assertEqual(compile_automation(RULE,spec(p),[TARGET],[],1)['mode'],'queued')
        self.assertEqual(compile_automation(RULE,spec(dusk()),[TARGET],[],1)['mode'],'single')

    def test_dawn_and_sunset_dusk_can_share_lamps_but_overlapping_and_unpredictable_rules_cannot(self):
        morning=dusk();morning.timing.start=type(morning.timing.start)(event='clock',at='06:00');morning.timing.end=type(morning.timing.end)(event='clock',at='12:00')
        self.assertTrue(separated(spec(morning),spec(dusk()),METADATA,'Europe/Berlin',NOW))
        overlap=copy.deepcopy(morning);overlap.timing.start.at='11:00';overlap.timing.end.at='14:00'
        self.assertFalse(separated(spec(morning),spec(overlap),METADATA,'Europe/Berlin',NOW))
        boundary=copy.deepcopy(morning);boundary.timing.start.at='12:00';boundary.timing.end.at='14:00'
        self.assertFalse(separated(spec(morning),spec(boundary),METADATA,'Europe/Berlin',NOW))

    def test_polar_missing_phase_and_dst_are_explicit_and_bounded(self):
        meta={'timezone':'Europe/Oslo','location':{'name':'Tromso','latitude':69.65,'longitude':18.96}}
        result=preview_program(spec(dusk()),[TARGET],[],meta,'Europe/Oslo','HA',True,datetime.fromisoformat('2026-06-21T12:00:00+00:00'))
        self.assertIsNone(result['timeline']);self.assertTrue(any('does not occur' in w for w in result['warnings']))
        for date,offset in [('2026-10-24',2),('2026-10-25',1)]:
            result=preview_program(spec(dusk()),[TARGET],[],METADATA,'Europe/Berlin','HA',True,datetime.fromisoformat(date+'T12:00:00+00:00'))
            self.assertEqual(datetime.fromisoformat(result['timeline']['start']).utcoffset(),timedelta(hours=offset))


if __name__=='__main__':unittest.main()
