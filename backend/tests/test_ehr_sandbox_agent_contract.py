"""Public env.step contract: large histories, recovery, all tools and write isolation."""
import copy
from urllib.parse import parse_qs,urlparse
import pytest
from tests.test_ehr_sandbox_runtime import task,env,call,patient
from asclepius.ehr_sandbox.env import EhrVisitEnv
from asclepius.ehr_sandbox.items import actual_items
from asclepius.ehr_sandbox.tools import all_tool_names
from asclepius.ehr_sandbox.common import document_text,extension
from asclepius.ehr_sandbox.visit_compiler import assert_no_worksheet_leak

SEARCHES=[('search_patients','Patient'),('list_encounters','Encounter'),('search_observations','Observation'),
          ('search_conditions','Condition'),('search_medications','MedicationRequest'),('search_allergies','AllergyIntolerance'),('search_documents','DocumentReference')]

@pytest.mark.parametrize('tool,kind',SEARCHES)
def test_all_history_pages_are_reachable_through_public_tools(task,tool,kind):
    pid=task['snapshot']['identifier']['value']
    original=next(e['resource'] for e in task['snapshot']['entry'] if e['resource']['resourceType']==kind and (kind!='MedicationRequest' or e['resource'].get('status')=='active'))
    for i in range(205):
        r=copy.deepcopy(original);r['id']=f'page-{i}'
        if kind!='Patient':r['subject']={'reference':'Patient/'+pid}
        task['snapshot']['entry'].append({'resource':r})
    e=EhrVisitEnv(task,max_steps=100);e.reset();params={'count':25}
    if kind!='Patient':params['patient_id']=pid
    found=[];cursor=None;total=None
    while True:
        result=call(e,tool,**params,**({'page_token':cursor} if cursor else {}))
        assert result['resourceType']=='Bundle',result
        total=result['total'];found.extend(r['resource']['id'] for r in result['entry'])
        if not result.get('link'):break
        cursor=parse_qs(urlparse(result['link'][0]['url']).query)['_page'][0]
    assert len(set(found))==len(found)==total and total>=205
    assert call(e,tool,**{**params,'count':26},page_token=cursor)['resourceType']=='OperationOutcome'


def test_encounter_filters_do_not_treat_finished_visits_as_open(env):
    result=call(env,'list_encounters',patient_id=patient(env),date_from='2031-03-01',date_to='2031-03-03')
    assert result['entry']
    assert all(r['resource']['period']['start'][:10]=='2031-03-03' for r in result['entry'])
    assert all(r['resource']['status']=='in-progress' for r in result['entry'])

@pytest.mark.parametrize('field', ['analyte','from_unit','to_unit'])
@pytest.mark.parametrize('value', [None,[],12,{}])
def test_calculator_malformed_fields_are_recoverable(env,field,value):
    args={'value':1,'analyte':'creatinine','from_unit':'mg/dL','to_unit':'umol/L'}
    assert call(env,'calculate',formula='unit_convert',inputs={**args,field:value})['resourceType']=='OperationOutcome'
    assert call(env,'calculate',formula='unit_convert',inputs=args)['value']==88.4
    assert call(env,'finish_visit')['finished']

@pytest.mark.parametrize('ending,expected',[('hold_medication','hold'),('discontinue_medication','stop'),('change_medication_order','decrease')])
def test_sequential_medication_changes_grade_actual_end_state(env,ending,expected):
    pid=patient(env);med=call(env,'search_medications',patient_id=pid)['entry'][0]['resource']
    changed=call(env,'change_medication_order',medication_request_id=med['id'],dose_value=2,dose_unit='mg',frequency='daily',reason='synthetic workflow')
    replacement=changed['entry'][1]['resource']
    args={'medication_request_id':replacement['id'],'reason':'synthetic reassessment'}
    if ending=='change_medication_order':args.update(dose_value=1,dose_unit='mg',frequency='daily')
    result=call(env,ending,**args);assert result['resourceType']!='OperationOutcome',result
    items=actual_items(list(env.sandbox._snapshot.values()),env.rollout()['overlay'],pid)
    meds=[i for i in items if i['type']=='med']
    assert len(meds)==1 and meds[0]['action']==expected


def test_notes_accept_only_known_synthetic_identity_and_trusted_order_dates(env):
    pid=patient(env);p=call(env,'get_patient',patient_id=pid)
    order=call(env,'create_lab_order',patient_id=pid,code='BMP',timing_days=7)
    header=f"Patient name: {p['name'][0]['text']}\nMRN: {p['identifier'][0]['value']}\nDOB: {p['birthDate']}"
    note=call(env,'write_visit_note',patient_id=pid,assessment=header,plan='Repeat BMP on '+order['occurrenceDateTime'][:10]+'.')
    assert note['resourceType']=='DocumentReference',note
    assert p['name'][0]['text'] in document_text(note)
    before=copy.deepcopy(env.sandbox.overlay)
    for extra in ('Patient name: Jane Identifier','MRN: 1234567899','Call 415-555-0199','Date: 1998-04-22'):
        result=call(env,'write_visit_note',patient_id=pid,assessment=header+'\n'+extra,plan='Review chart.')
        assert result['resourceType']=='OperationOutcome' and env.sandbox.overlay==before


def test_historical_boilerplate_allowed_only_with_immutable_source_lineage():
    import base64
    text='Continue current therapy and repeat the chemistry panel at follow up.'
    old={'resourceType':'DocumentReference','id':'prior','extension':[extension('offsetDays',-90)],
         'content':[{'attachment':{'data':base64.b64encode(text.encode()).decode()}}]}
    assert_no_worksheet_leak([old],text,source_resources=[old],worksheet_ids=['current'],day=0)
    for sources,ids in [([],['current']),([old],['prior']),([{**old,'extension':[extension('offsetDays',0)]}],['current'])]:
        with pytest.raises(ValueError,match='overlap'):
            assert_no_worksheet_leak([old],text,source_resources=sources,worksheet_ids=ids,day=0)
    tampered=copy.deepcopy(old);tampered['content'][0]['attachment']['data']=base64.b64encode((text+' New current decision.').encode()).decode()
    with pytest.raises(ValueError,match='overlap'):
        assert_no_worksheet_leak([tampered],text,source_resources=[old],worksheet_ids=['current'],day=0)


def test_all_twenty_four_tools_work_through_public_action_contract(task):
    e=EhrVisitEnv(task,max_steps=80);e.reset();used=set()
    def act(tool,**params):
        result=call(e,tool,**params);used.add(tool)
        assert result.get('resourceType')!='OperationOutcome',(tool,result)
        return result
    matches=act('search_patients');p=matches['entry'][0]['resource'];pid=p['id']
    # Select the specified target, never rely on the first search result.
    target=task['snapshot']['identifier']['value'];p=next(x['resource'] for x in matches['entry'] if x['resource']['id']==target);pid=p['id']
    act('get_patient',patient_id=pid)
    for tool,_ in SEARCHES[1:]:act(tool,patient_id=pid)
    documents=act('search_documents',patient_id=pid)
    act('read_document',document_id=documents['entry'][0]['resource']['id'])
    act('calculate',formula='ckd_stage',inputs={'egfr':28})
    med=act('create_medication_order',patient_id=pid,drug='amlodipine',dose_value=5,dose_unit='mg',route='oral',frequency='daily')
    change=act('change_medication_order',medication_request_id=med['id'],dose_value=2.5,reason='synthetic change')
    latest=change['entry'][1]['resource']['id']
    act('hold_medication',medication_request_id=latest,reason='synthetic hold')
    act('discontinue_medication',medication_request_id=latest,reason='synthetic stop')
    act('create_lab_order',patient_id=pid,code='BMP',timing_days=7)
    act('create_referral',patient_id=pid,specialty='dietitian',reason='education')
    act('create_imaging_order',patient_id=pid,study='renal ultrasound',reason='synthetic workup')
    act('schedule_follow_up',patient_id=pid,interval_days=28)
    act('search_orders',patient_id=pid,count=1)
    act('record_assessment',patient_id=pid,text='CKD stage4',icd10='N18.4')
    act('flag_urgent',patient_id=pid,action='same_day_contact',reason='synthetic routing')
    act('write_visit_note',patient_id=pid,assessment='CKD stage4',plan='Repeat BMP in7 days.')
    act('finish_visit');assert e.terminated and not e.truncated
    probe=EhrVisitEnv({**task,'task_kind':'probe_retrieval'});probe.reset()
    assert call(probe,'submit_answer',answer={'value':28})['submitted'];used.add('submit_answer')
    assert used==set(all_tool_names())

@pytest.mark.parametrize('suffix',[' Jane Identifier',' / Jane Identifier',', Jane Identifier','; Jane Identifier'])
def test_synthetic_name_cannot_mask_appended_unknown_identity(env,suffix):
    p=call(env,'get_patient',patient_id=patient(env))
    result=call(env,'write_visit_note',patient_id=p['id'],assessment='Patient name: '+p['name'][0]['text']+suffix,plan='Review.')
    assert result['resourceType']=='OperationOutcome' and not env.sandbox.overlay


def test_synthetic_date_formats_and_doctor_names(env):
    from datetime import date
    p=call(env,'get_patient',patient_id=patient(env));dob=date.fromisoformat(p['birthDate']).strftime('%m/%d/%Y')
    assert call(env,'write_visit_note',patient_id=p['id'],assessment='Date: 03/03/2031\nDOB: '+dob,plan='Review.')['resourceType']=='DocumentReference'
    assert call(env,'write_visit_note',patient_id=p['id'],assessment='Dr. '+p['name'][0]['text']+' Jane Identifier',plan='Review.')['resourceType']=='OperationOutcome'


def test_prior_structured_assessment_requires_exact_source(env):
    text='Chronic kidney disease is progressing and requires continued close monitoring.'
    old={'resourceType':'Condition','id':'prior-assessment','extension':[extension('offsetDays',-90)],'code':{'text':text}}
    assert_no_worksheet_leak([old],text,source_resources=[old],day=0)
    with pytest.raises(ValueError):
        assert_no_worksheet_leak([{**old,'code':{'text':text+' New plan.'}}],text,source_resources=[old],day=0)


@pytest.mark.parametrize('frequency',['qhs','nightly','every 12 hours','every other day'])
def test_partial_edit_preserves_known_text_frequency(task,frequency):
    pid=task['snapshot']['identifier']['value']
    med=next(x['resource'] for x in task['snapshot']['entry'] if x['resource']['resourceType']=='MedicationRequest' and x['resource'].get('status')=='active' and x['resource']['subject']['reference']=='Patient/'+pid)
    med['dosageInstruction']=[{'text':'20 mg oral '+frequency}]
    e=EhrVisitEnv(task);e.reset()
    result=call(e,'change_medication_order',medication_request_id=med['id'],dose_value=10,reason='Synthetic dose adjustment')
    assert result['resourceType']=='Bundle',result
    assert frequency in result['entry'][1]['resource']['dosageInstruction'][0]['text']

@pytest.mark.parametrize('tool,args',[
 ('create_medication_order',{'drug':'lisinopril','dose_value':5,'dose_unit':'mg','frequency':'daily','route':'oral'}),
 ('create_lab_order',{'code':'BMP'}),('create_referral',{'specialty':'dietitian','reason':'Review'}),
 ('create_imaging_order',{'study':'renal ultrasound','reason':'Review'}),('schedule_follow_up',{'interval_days':28}),
 ('record_assessment',{'text':'CKD stage4','icd10':'N18.4'}),('flag_urgent',{'action':'same_day_contact','reason':'Review'}),
 ('write_visit_note',{'assessment':'Synthetic review','plan':'Follow up.'}),
 ('hold_medication',{'reason':'Review'}),('discontinue_medication',{'reason':'Review'}),
 ('change_medication_order',{'reason':'Review','dose_value':2,'dose_unit':'mg','frequency':'daily'}),
])
def test_all_mutation_families_reject_decoys_without_partial_writes(env,tool,args):
    decoy=next(r['id'] for r in env.sandbox.resources() if r['resourceType']=='Patient' and r['id']!=patient(env))
    if tool in ('hold_medication','discontinue_medication','change_medication_order'):
        med=call(env,'search_medications',patient_id=decoy)['entry'][0]['resource']
        params={'medication_request_id':med['id'],**args}
    else:params={'patient_id':decoy,**args}
    before=env.sandbox.resources()
    result=call(env,tool,**params)
    assert result['resourceType']=='OperationOutcome' and env.sandbox.wrong_patient
    assert env.sandbox.resources()==before and not env.sandbox.overlay
    assert call(env,'create_lab_order',patient_id=patient(env),code='BMP')['resourceType']=='ServiceRequest'


def test_order_pagination_and_query_binding(task):
    pid=task['snapshot']['identifier']['value']
    for i in range(205):
        task['snapshot']['entry'].append({'resource':{'resourceType':'ServiceRequest','id':f'order-{i}',
            'status':'active','intent':'order','subject':{'reference':'Patient/'+pid},'code':{'text':'BMP'},
            'category':[{'text':'lab'}],'authoredOn':'2031-03-02T09:00:00-08:00'}})
    e=EhrVisitEnv(task,max_steps=100);e.reset();found=[];cursor=None
    while True:
        result=call(e,'search_orders',patient_id=pid,kind='lab',count=25,**({'page':cursor} if cursor else {}))
        assert result['resourceType']=='Bundle'
        found.extend(r['resource']['id'] for r in result['entry'])
        if not result.get('link'):break
        cursor=parse_qs(urlparse(result['link'][0]['url']).query)['page'][0]
    assert len(found)==len(set(found))==result['total'] and len(found)>=205
    assert call(e,'search_orders',patient_id=pid,kind='all',count=25,page=cursor)['resourceType']=='OperationOutcome'

@pytest.mark.parametrize('formula,inputs',[
 ('unit_convert',{'value':1e308,'analyte':'creatinine','from_unit':'mg/dL','to_unit':'umol/L'}),
 ('bmi',{'height_cm':1e-300,'weight_kg':70}),
])
def test_calculator_arithmetic_errors_preserve_episode(env,formula,inputs):
    assert call(env,'calculate',formula=formula,inputs=inputs)['resourceType']=='OperationOutcome'
    assert call(env,'calculate',formula='ckd_stage',inputs={'egfr':28})['value']=='G4'


def test_negative_note_cannot_document_placed_orders(env):
    from asclepius.ehr_sandbox.grader import note_check
    pid=patient(env)
    call(env,'create_lab_order',patient_id=pid,code='BMP',timing_days=7)
    call(env,'schedule_follow_up',patient_id=pid,interval_days=28)
    call(env,'write_visit_note',patient_id=pid,assessment='Chart reviewed.',plan='Do not order BMP. Do not follow up in 28 days.')
    snapshot=list(env.sandbox._snapshot.values());overlay=env.rollout()['overlay']
    result=note_check(snapshot,overlay,pid,actual_items(snapshot,overlay,pid))
    assert result['consistency']<1 and any(i['status']=='undocumented_action' for i in result['items'])
