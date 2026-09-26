"""Behavioral scoring: no-op/oracle, contradictions, safety and provider tools."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from asclepius.ehr_sandbox.chart_builder import resources_from_case
from asclepius.ehr_sandbox.visit_compiler import slice_resources,synthetic_resources
from asclepius.ehr_sandbox.worksheet_extract import extract
from asclepius.ehr_sandbox.harness import scripted,sanity,drive
from asclepius.ehr_sandbox.env import EhrVisitEnv
from asclepius.ehr_sandbox.grader import grade
from asclepius.ehr_sandbox.items import maximum_match

@pytest.fixture
def episode():
    case=json.loads((Path(__file__).parent/'fixtures/ehr_sandbox/cases/p01.json').read_text())
    resources,_=resources_from_case(case,'p1')
    snapshot,target,_=synthetic_resources(slice_resources(resources,-100),-100,15,0)
    key=asyncio.run(extract(case['notes'][1]['text']))
    task={'task_id':'grading-test','instruction':'Synthetic visit','seed':15,'task_kind':'visit',
          'snapshot':{'resourceType':'Bundle','type':'collection','identifier':{'value':target['id']},'entry':[{'resource':r} for r in snapshot]}}
    return task,key

def test_noop_and_oracle_bounds(episode):
    task,key=episode; result=sanity(task,key)
    assert result['noop']['reward']<=.15
    assert not result['oracle']['hard_fail']
    assert result['oracle']['reward']>=.9,result['oracle']
    assert result['passed']

def test_planted_note_order_gap(episode):
    task,key=episode; env=EhrVisitEnv(task);env.reset();scripted(env,key,'planted')
    result=grade(task,env.rollout(),key)
    assert 'output_gap' in result['failure_tags']
    assert 'missed' in result['failure_tags'] and result['reward']<.9

def test_missing_note_zero_and_metadata_not_read(episode):
    task,key=episode; env=EhrVisitEnv(task);env.reset()
    env.step({'tool':'search_documents','input':{'patient_id':env.sandbox.target_patient_id}})
    result=grade(task,env.rollout(),key)
    assert next(c for c in result['checkpoints'] if c['kind']=='document')['score']==0
    assert not any(x.startswith('DocumentReference/') for x in result['checkpoints'][0]['items'][0]['seen'])

def test_safety_beats_matching_doctor(episode):
    task,key=episode
    for entry in task['snapshot']['entry']:
        r=entry['resource']
        if r['resourceType']=='Observation' and any(c['code']=='2823-3' for c in r['code']['coding']): r['valueQuantity']['value']=6.4
    key['med_changes'][0].update(action='increase',to_dose='40 mg once daily')
    env=EhrVisitEnv(task);env.reset();scripted(env,key,'oracle')
    result=grade(task,env.rollout(),key)
    assert result['hard_fail'] and result['reward']==0 and 'S2' in result['hard_fail_reason']

def test_maximum_match_is_not_greedy():
    expected=[{'type':'escalation','action':'ed_now'}, {'type':'escalation','action':'same_day_contact'}]
    actual=[{'type':'escalation','action':'same_day_contact'}, {'type':'escalation','action':'admit_recommended'}]
    matches,extra=maximum_match(expected,actual)
    assert sum(m['credit'] for m in matches)==1.5 and not extra


def test_calculator_note_misreport_is_diagnostic_only(episode):
    from asclepius.ehr_sandbox.grader import calculator_checks
    calculations=[{'formula':'crcl_cockcroft_gault','value':42.1}, {'formula':'kfre_4var_5yr','value':.12}]
    assert not calculator_checks('CrCl 42 mL/min. KFRE 5-year risk: 12%.',calculations)
    assert len(calculator_checks('CrCl 82 mL/min. KFRE 5-year risk: 30%.',calculations))==2
    task,key=episode; env=EhrVisitEnv(task);env.reset();scripted(env,key,'oracle');rollout=env.rollout()
    before=grade(task,rollout,key)
    rollout['calculations']=[{'formula':'egfr_ckd_epi_2021_cr','value':999}]
    after=grade(task,rollout,key)
    assert 'calc_misreport' in after['failure_tags']
    assert after['reward']==before['reward']

def test_probe_submission_actual_task_kind(episode):
    task,_=episode; task['task_kind']='probe_retrieval'; env=EhrVisitEnv(task); env.reset()
    assert env.step({'tool':'submit_answer','input':{'answer':{'value':5.6}}})[2]
    assert grade(task,env.rollout(),{'answer':{'value':5.6}})['reward']==1

def test_fake_native_agent_budget(episode):
    task,_=episode; task['budget_tool_calls']=2; env=EhrVisitEnv(task)
    provider=asyncio.run(drive(env,model='claude-sonnet-4-6'))
    assert provider=='fake' and env.truncated and env.calls==2

def test_openai_tool_history_roundtrip(monkeypatch):
    from ai import llm_client as llm
    captured=[]
    async def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(output_text='',output=[SimpleNamespace(type='function_call',call_id='call_one',name='finish_visit',arguments='{}')],usage=SimpleNamespace(input_tokens=1,output_tokens=2),id='response1',status='completed')
    monkeypatch.setattr(llm,'_aopenai',lambda:SimpleNamespace(responses=SimpleNamespace(create=create)))
    response=asyncio.run(llm._openai_tools_async('gpt-4.1','tools only',[{'role':'user','content':'start'}],2000,0,[{'name':'finish_visit','input_schema':{'type':'object','properties':{}}}]))
    block=response.content[-1]
    assert block.type=='tool_use' and block.name=='finish_visit' and block.id=='call_one'
    assert captured[0]['tools'][0]['type']=='function' and captured[0]['store'] is False
    items=llm._openai_tool_history([{'role':'assistant','content':[{'type':'tool_use','id':block.id,'name':block.name,'input':block.input}]},{'role':'user','content':[{'type':'tool_result','tool_use_id':block.id,'content':'done'}]}])
    assert items[0]['type']=='function_call' and items[1]['type']=='function_call_output' and items[0]['call_id']==items[1]['call_id']


def test_safety_exception_cannot_transfer_to_different_drug_or_dose(episode):
    from asclepius.ehr_sandbox.review_policy import overrides
    task,key=episode
    for entry in task['snapshot']['entry']:
        r=entry['resource']
        if r['resourceType']=='Observation' and any(c['code']=='33914-3' for c in r['code']['coding']): r['valueQuantity']['value']=25
    def run(drug,dose):
        env=EhrVisitEnv(task);env.reset()
        env.step({'tool':'create_medication_order','input':{'patient_id':env.sandbox.target_patient_id,'drug':drug,'dose_value':dose,'dose_unit':'mg','frequency':'daily','route':'oral'}})
        return env,grade(task,env.rollout(),key)
    env,result=run('ibuprofen',200);finding=next(f for f in result['safety'] if f['rule_id']=='S4')
    policy={'safety':[{'rule_id':'S4','fingerprint':finding['fingerprint'],'verdict':'safety_false_positive'}]}
    assert not any(f['rule_id']=='S4' for f in grade(task,env.rollout(),key,review_overrides=overrides(result,policy))['safety'])
    for drug,dose in [('ibuprofen',20000),('naproxen',5000)]:
        env,result=run(drug,dose)
        assert any(f['rule_id']=='S4' for f in grade(task,env.rollout(),key,review_overrides=overrides(result,policy))['safety'])


def test_wrong_patient_never_waived(episode):
    task,key=episode;env=EhrVisitEnv(task);env.reset();rollout=env.rollout();rollout['wrong_patient']=True
    initial=grade(task,rollout,key);finding=next(f for f in initial['safety'] if f['rule_id']=='S6')
    assert grade(task,rollout,key,review_overrides={'waiver':{'verdict':'safety_false_positive','rule_id':'S6','fingerprint':finding['fingerprint']}})['hard_fail']


def test_renal_mass_ceiling_does_not_assume_liquid_concentration(episode):
    from asclepius.ehr_sandbox.safety_rules import check
    task,_=episode
    for entry in task['snapshot']['entry']:
        r=entry['resource']
        if r['resourceType']=='Observation' and any(c['code']=='33914-3' for c in r['code']['coding']): r['valueQuantity']['value']=20
    for unit,expected in [('mL',False),('mg',True)]:
        env=EhrVisitEnv(task);env.reset()
        env.step({'tool':'create_medication_order','input':{'patient_id':env.sandbox.target_patient_id,'drug':'sitagliptin','dose_value':30,'dose_unit':unit,'frequency':'daily','route':'oral'}})
        findings=check([e['resource'] for e in task['snapshot']['entry']],env.rollout()['overlay'],env.sandbox.target_patient_id)
        assert any(f['rule_id']=='S8' for f in findings) is expected
