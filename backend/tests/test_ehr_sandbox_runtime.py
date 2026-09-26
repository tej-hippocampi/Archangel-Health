"""Adversarial runtime contracts: isolation, real writes, search, budget and privacy."""
import base64
import copy
import json
from pathlib import Path
import pytest
from asclepius.ehr_sandbox.chart_builder import resources_from_case
from asclepius.ehr_sandbox.visit_compiler import slice_resources,synthetic_resources,assert_fhir_slice,assert_no_worksheet_leak
from asclepius.ehr_sandbox.env import EhrVisitEnv
from asclepius.ehr_sandbox.common import validate,document_text
from asclepius.ehr_sandbox.calculators import calculate

@pytest.fixture
def task():
    case=json.loads((Path(__file__).parent/'fixtures/ehr_sandbox/cases/p01.json').read_text())
    resources,_=resources_from_case(case,'p1'); visible=slice_resources(resources,0)
    assert_fhir_slice(visible,0)
    snapshot,target,_=synthetic_resources(visible,0,177,0)
    decoy,_,_=synthetic_resources(visible,0,177,1,family=target['name'][0]['family'])
    return {'task_id':'test-task','instruction':'Synthetic visit','seed':177,'env_version':'test',
            'snapshot':{'resourceType':'Bundle','type':'collection','identifier':{'value':target['id']},'entry':[{'resource':r} for r in snapshot+decoy]}}

@pytest.fixture
def env(task):
    e=EhrVisitEnv(task);e.reset();return e

def call(env,tool_name,**args): return env.step({'tool':tool_name,'input':args})[0]['observation']
def patient(env): return env.sandbox.target_patient_id

def test_opening_and_state_no_chart_or_key(task):
    task.update(key_enc='CANARYKEY',outcome_enc='FUTURECANARY')
    e=EhrVisitEnv(task); obs,info=e.reset()
    assert set(obs)=={'instruction','now'} and len(info['tool_schemas'])==24
    assert 'CANARY' not in json.dumps(e.task) and 'CANARY' not in e.render()

def test_pagination_dates_groups_and_unknown_params(env):
    pid=patient(env); s=env.sandbox
    allrows=s.search('Observation',patient=pid,**{'code:group':'renal','_count':200})
    assert allrows['total']>=6
    first=s.search('Observation',patient=pid,**{'code:group':'renal','_count':1})
    token=first['link'][0]['url'].split('=',1)[1]
    second=s.search('Observation',patient=pid,**{'code:group':'renal','_count':1,'_page':token})
    assert first['entry'][0]['resource']['id']!=second['entry'][0]['resource']['id']
    assert s.search('Observation',patient=pid,**{'_page':token})['resourceType']=='OperationOutcome'
    assert s.search('Observation',patient=pid,bogus='x')['issue'][0]['code']=='not-supported'
    empty=s.search('Observation',patient=pid,date='gt2031-03-03')
    assert empty['total']==0

def test_patient_search_and_document_metadata(env):
    p=call(env,'get_patient',patient_id=patient(env))
    matches=call(env,'search_patients',name=p['name'][0]['family'].upper())
    assert matches['total']==2
    assert call(env,'search_patients',identifier=p['identifier'][0]['value'])['total']==1
    docs=call(env,'search_documents',patient_id=patient(env))
    assert docs['entry'] and all('content' not in r['resource'] for r in docs['entry'])
    text=call(env,'read_document',document_id=docs['entry'][0]['resource']['id'])
    assert text['text']

@pytest.mark.parametrize('tool,extra',[
 ('create_medication_order',{'drug':'lisinopril','dose_value':5,'dose_unit':'mg','route':'oral','frequency':'once daily'}),
 ('create_lab_order',{'code':'BMP','timing_days':7}),
 ('create_referral',{'specialty':'vascular_surgery','reason':'access planning'}),
 ('create_imaging_order',{'study':'renal ultrasound','reason':'obstruction assessment'}),
 ('schedule_follow_up',{'interval_days':28}),
 ('record_assessment',{'icd10':'N18.4','text':'CKD stage 4','ckd_stage':'G4'}),
 ('flag_urgent',{'action':'same_day_contact','reason':'urgent abnormal chemistry'}),
 ('write_visit_note',{'assessment':'CKD stage 4','plan':'Review medications and repeat chemistry.'}),
])
def test_all_write_tools_validate_and_persist(env,tool,extra):
    result=call(env,tool,patient_id=patient(env),**extra)
    assert result['resourceType']!='OperationOutcome',result
    for r in env.sandbox.overlay.values(): validate(r)
    assert env.sandbox.overlay
    if result['resourceType'] in ('MedicationRequest','ServiceRequest'): assert result['authoredOn']=='2031-03-03T09:00:00-08:00'

def test_medication_change_atomic_and_copy_on_write(env):
    med=call(env,'search_medications',patient_id=patient(env))['entry'][0]['resource']
    result=call(env,'change_medication_order',medication_request_id=med['id'],dose_value=5,dose_unit='mg',frequency='once daily',reason='monitor potassium')
    assert result['resourceType']=='Bundle',result
    active=call(env,'search_medications',patient_id=patient(env))['entry'][0]['resource']
    assert active['priorPrescription']['reference']=='MedicationRequest/'+med['id']
    assert env.sandbox._snapshot['MedicationRequest/'+med['id']]['status']=='active'
    assert env.sandbox.overlay['MedicationRequest/'+med['id']]['status']=='stopped'
    before=copy.deepcopy(env.sandbox.overlay)
    bad=call(env,'change_medication_order',medication_request_id=active['id'],dose_value=-1,reason='invalid')
    assert bad['resourceType']=='OperationOutcome' and env.sandbox.overlay==before
    held=call(env,'hold_medication',medication_request_id=active['id'],reason='potassium')
    assert held['status']=='on-hold'
    stopped=call(env,'discontinue_medication',medication_request_id=active['id'],reason='replace treatment')
    assert stopped['status']=='stopped'

def test_wrong_patient_rejected_hard_flag(env):
    other=next(r['id'] for r in env.sandbox.resources() if r['resourceType']=='Patient' and r['id']!=patient(env))
    result=call(env,'create_lab_order',patient_id=other,code='BMP')
    assert result['resourceType']=='OperationOutcome' and env.sandbox.wrong_patient and not env.sandbox.overlay

def test_read_scrub_blocks_phi_in_base64(task):
    doc=next(e['resource'] for e in task['snapshot']['entry'] if e['resource']['resourceType']=='DocumentReference')
    doc['content'][0]['attachment']['data']=base64.b64encode(b'Contact test@example.com').decode()
    env=EhrVisitEnv(task);env.reset()
    assert call(env,'read_document',document_id=doc['id'])['resourceType']=='OperationOutcome'


def test_repeated_episodes_share_immutable_index(task):
    a=EhrVisitEnv(task);b=EhrVisitEnv(task);a.reset();b.reset()
    assert a.sandbox._snapshot is b.sandbox._snapshot
    ref=next(iter(a.sandbox._snapshot))
    with pytest.raises(TypeError): a.sandbox._snapshot[ref]['id']='changed'
    call(a,'create_lab_order',patient_id=patient(a),code='BMP')
    assert not b.sandbox.overlay
    baseline=a.sandbox._snapshot;a.reset()
    assert a.sandbox._snapshot is baseline and not a.sandbox.overlay


def test_budget_thoughts_and_replay(task):
    e=EhrVisitEnv(task,max_steps=2);e.reset()
    for _ in range(100): e.step({'type':'thought','content':'reason'})
    assert e.calls==0
    action={'tool':'search_patients','input':{}}
    obs=e.step(action); last=e.step(action)
    assert last[3] and e.terminated_by=='budget'
    e.reset(); assert e.step(action)[0]==obs[0]
    assert e.step({'tool':'finish_visit','input':{}})[2]
    assert not e.sandbox.overlay

def test_invalid_terminal_does_not_terminate(env):
    call(env,'submit_answer',answer={'x':1})
    assert not env.terminated
    call(env,'finish_visit',unexpected=1)
    assert not env.terminated

def test_worksheet_leak_guard_reads_encoded_documents(env):
    docs=[r for r in env.sandbox.resources() if r['resourceType']=='DocumentReference']
    with pytest.raises(ValueError,match='overlap'): assert_no_worksheet_leak(docs,document_text(docs[0]))

@pytest.mark.parametrize('g,stage',[(90,'G1'),(60,'G2'),(59,'G3a'),(45,'G3a'),(44,'G3b'),(30,'G3b'),(29,'G4'),(15,'G4'),(14,'G5')])
def test_gfr_boundaries(g,stage): assert calculate('ckd_stage',{'egfr':g})['value']==stage

def test_calculator_published_equations():
    """NIDDK 2021 equations; Cockcroft/Gault Nephron 1976: 72yo,72kg,Cr1 =>68."""
    assert calculate('crcl_cockcroft_gault',{'age':72,'sex':'male','weight_kg':72,'creatinine_mg_dl':1})['value']==68
    assert calculate('egfr_ckd_epi_2021_cr',{'age':50,'sex':'male','creatinine_mg_dl':1})['value']==pytest.approx(91.691479,abs=1e-6)
    assert calculate('unit_convert',{'value':1,'from_unit':'mg/dL','to_unit':'µmol/L','analyte':'creatinine'})['value']==88.4
    two=calculate('kfre_4var_2yr',{'age':70.36,'sex':'male','egfr':36.11,'uacr_mg_g':170.204})['value']
    five=calculate('kfre_4var_5yr',{'age':70.36,'sex':'male','egfr':36.11,'uacr_mg_g':170.204})['value']
    assert 0<two<five<1
    with pytest.raises(ValueError): calculate('egfr_ckd_epi_2021_cr',{'age':50,'sex':'male','creatinine_mg_dl':0})
    with pytest.raises(ValueError): calculate('egfr_ckd_epi_2021_cr',{'age':50,'sex':'male','creatinine_mg_dl':1,'race':'x'})


def test_non_finite_or_deeply_nested_arguments_are_recoverable_tool_errors(env):
    pid=patient(env)
    bad=env.step({'tool':'calculate','input':{'operation':'egfr_ckd_epi_2021','creatinine':float('nan')}})[0]['observation']
    assert bad['resourceType']=='OperationOutcome'
    deep={};node=deep
    for _ in range(900): node['x']={};node=node['x']
    assert env.step({'tool':'submit_answer','input':{'answer':deep}})[0]['observation']['resourceType']=='OperationOutcome'
    assert env.calls==2 and not env.terminated
    from asclepius.ehr_sandbox.common import dumps
    dumps(env.rollout()['trajectory'])  # the trajectory stays storable
    assert call(env,'get_patient',patient_id=pid)['id']==pid


def test_status_update_keeps_the_prescription_authored_date(env):
    pid=patient(env)
    med=call(env,'search_medications',patient_id=pid)['entry'][0]['resource']
    call(env,'discontinue_medication',medication_request_id=med['id'],reason='test')
    updated=env.sandbox.overlay['MedicationRequest/'+med['id']]
    assert updated['status']!='active' and updated.get('authoredOn')==med.get('authoredOn')


@pytest.mark.parametrize('text,frequency,value',[('1,000 mg','twice daily',2000.0),('3 mg','three times weekly',3*3/7),
    ('2 mg','twice weekly',2*2/7),('10 mg','every morning',10.0),('10 mg','once weekly',10/7)])
def test_renal_dosing_amounts_parse(text,frequency,value):
    from asclepius.ehr_sandbox.terminology import daily_amount
    assert daily_amount(text,frequency)['value']==pytest.approx(value)


def test_grader_token_check_rejects_non_ascii_without_crashing():
    from fastapi.testclient import TestClient
    from asclepius.ehr_sandbox.server import create_grader_app
    client=TestClient(create_grader_app(tasks={},keys={},token='secret'))
    response=client.post('/grade',json={'task_id':'x','seed':0,'actions':[]},headers=[(b'authorization','Bearer sécret'.encode('utf-8'))])
    assert response.status_code==403
