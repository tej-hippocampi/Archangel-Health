"""Independent-audit regressions: clinical time, demographics and review evidence."""
import asyncio
import copy
import json
from datetime import date

import pytest
from asclepius.ehr_sandbox import chart_builder, identities, outcome_rules, reviews, visit_compiler
from asclepius.ehr_sandbox.common import document_text, dumps, ext, extension, resource_ref
from asclepius.ehr_sandbox.constants import ANCHOR
from asclepius.ehr_sandbox.sealed import seal
from asclepius.ehr_sandbox.worksheet_extract import extract
from tests.test_ehr_sandbox_workflow import store, compiled, candidate


def test_synthetic_age_honors_both_band_limits_and_confusable_birth_year():
    for seed in range(100):
        generated=identities.identity(seed,0,'65-69')
        dob=date.fromisoformat(generated['birthDate'])
        age=ANCHOR.year-dob.year-((ANCHOR.month,ANCHOR.day)<(dob.month,dob.day))
        assert 65<=age<=69
    with pytest.raises(ValueError,match='incompatible_decoy'):
        identities.identity(1,2,'80-89',birth_year=2000)


def peer(chart_id,band='65-69',gender='male',egfr=25):
    patient={'resourceType':'Patient','id':chart_id,'gender':gender,'extension':[extension('ageBand',band)]}
    observation={'resourceType':'Observation','id':chart_id+'-egfr','status':'final','subject':{'reference':'Patient/'+chart_id},
        'code':{'coding':[{'system':'http://loinc.org','code':'33914-3'}]},
        'valueQuantity':{'value':egfr,'unit':'mL/min/1.73m2'},'extension':[extension('offsetDays',-1)]}
    return {'chart_id':chart_id,'resources_json':dumps([patient,observation]),'extraction_json':dumps({'visits':[{'offset_days':0}]})}


def test_decoys_select_compatible_sources_and_a_matching_ckd_stage():
    target={**identities.identity(6,0,'65-69','male'),'id':'target'}
    source=peer('target'); resources=json.loads(source['resources_json'])
    peers=[peer('unrelated','80-89','female'),peer('family',egfr=70),peer('same-age'),peer('same-stage')]
    chosen=visit_compiler.decoy_peers(peers,0,6,target,resources,third=True)
    assert len({row[0]['chart_id'] for row in chosen})==3
    assert chosen[1][3]['gender']=='male'
    assert ext(chosen[1][3],'ageBand')=='65-69'
    assert visit_compiler._ckd_stage(chosen[2][2])=='G4'
    with pytest.raises(ValueError,match='age_sex'):
        visit_compiler.decoy_peers([peer('a','80-89','female'),peer('b','80-89','female')],0,6,target,resources)


def medication_history(start,stop):
    return chart_builder.resources_from_case({'demographics':{'age_band':'65-69','sex':'male'},'medication_events':[
        {'drug':'lisinopril','action':'start','dose':'10 mg daily','collected_offset_days':start},
        {'drug':'lisinopril','action':'stop','reason':'AKI','collected_offset_days':stop}]},'patient')[0]


def test_outcome_retains_dated_stop_reason_and_excludes_later_stop():
    key={'med_changes':[{'drug':'lisinopril','action':'start'}]}
    resources=medication_history(0,30)
    outcome=visit_compiler.outcome_window(resources,0,30)
    assert any(ext(r)==30 and r.get('statusReason',{}).get('text')=='AKI' for r in outcome['resources'])
    assert any(f['rule_id']=='O4' for f in outcome_rules.evaluate([],outcome,key))
    resources=medication_history(10,60)
    earlier=visit_compiler.outcome_window(resources,0,30)
    assert earlier['resources'][0]['status']=='active'
    assert not earlier['resources'][0].get('statusReason')
    assert not outcome_rules.evaluate([],earlier,key)


def observation(identifier,day,value,unit):
    return {'resourceType':'Observation','id':identifier,'subject':{'reference':'Patient/p'},
        'code':{'coding':[{'code':'2160-0'}]},'valueQuantity':{'value':value,'unit':unit},'extension':[extension('offsetDays',day)]}


def test_outcome_comparison_normalizes_units_and_sorts_baseline_time():
    assert not outcome_rules.evaluate([observation('a',-1,1,'mg/dL')],
        {'resources':[observation('b',20,88.4,'µmol/L')]},{})
    before=[observation('recent',-1,2,'mg/dL'),observation('older',-30,1,'mg/dL')]
    assert not outcome_rules.evaluate(before,{'resources':[observation('after',20,2,'mg/dL')]},{})
    assert any(f['rule_id']=='O2' for f in outcome_rules.evaluate(before,{'resources':[observation('worse',20,3,'mg/dL')]},{}))


def test_outcome_references_use_the_same_synthetic_ids_as_the_previsit_chart():
    resources=medication_history(-10,20)
    visible=visit_compiler.slice_resources(resources,0)
    snapshot,_,_=visit_compiler.synthetic_resources(visible,0,6,0)
    patient=next(r for r in resources if r['resourceType']=='Patient')
    future=visit_compiler.outcome_window(resources,0,30)['resources']
    displayed,_,_=visit_compiler.synthetic_resources([patient]+future,0,6,0,reference_resources=resources)
    refs={resource_ref(r) for r in snapshot}
    assert all(r['priorPrescription']['reference'] in refs for r in displayed if r.get('priorPrescription'))


def test_key_correction_preserves_structured_source_and_rejects_unrelated_med_quote(store,compiled,monkeypatch):
    task,chart=compiled
    note='Follow up in 42 days.'
    key=asyncio.run(extract(note))
    source={'item_id':'structured-med-0','action':'decrease','drug':'Prinivil','to_dose':'10 mg daily',
            'source':'structured','confidence':.95,'rxnorm_ingredient':'29046'}
    key['med_changes']=[source]
    visit={'key_enc':seal(key),'encounter_ref':'enc'}
    import base64
    chart={'resources_json':dumps([{'resourceType':'DocumentReference','id':'doc','content':[{'attachment':{'data':base64.b64encode(note.encode()).decode()}}]}]),
           'extraction_json':dumps({'visits':[{'encounter_ref':'enc','document_ids':['doc']}]})}
    monkeypatch.setattr(reviews,'_chart',lambda *a:(visit,chart))
    row={'visit_id':'visit'}; replacement=copy.deepcopy(key);replacement['follow_up']['tolerance_days']=12
    audit={'status':'corrected','replacement_key':replacement}
    assert reviews._key_audit_vote(store,row,audit,None)['key-audit'].startswith('key_corrected:')
    assert audit['replacement_key']['med_changes']==[source]
    replacement=copy.deepcopy(key); replacement['med_changes']=[{'drug':'warfarin','action':'increase','to_dose':'500 mg daily','source_span':note,'confidence':1}]
    with pytest.raises(ValueError,match='supporting the drug'):
        reviews._key_audit_vote(store,row,{'status':'corrected','replacement_key':replacement},None)


def test_review_offer_has_absolute_realm_specific_link(store,compiled,monkeypatch):
    import notifications, realm
    task,_=compiled; messages=[]
    monkeypatch.setenv('ASCLEPIUS_PORTAL_URL','https://portal.example/asclepius')
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('reviewer')])
    monkeypatch.setattr(notifications,'notify_person',lambda *a,**kw:messages.append(kw))
    with realm.scoped('sandbox'):
        review=reviews.create_review(task['visit_id'],'disagreement',[{'item_id':'link','key':{'type':'lab','group':'BMP'},'actual':None}],store=store)
    assert f'https://portal.example/sandbox/asclepius#ehr-review/{review["review_id"]}' in messages[0]['body_html']


def test_decoy_role_assignment_does_not_consume_the_only_stage_match():
    target={**identities.identity(3,0,'65-69','male'),'id':'target'}
    resources=json.loads(peer('target')['resources_json'])
    peers=[peer('only-stage-match'),peer('other-compatible',egfr=70),peer('family','80-89','female',70)]
    result=visit_compiler.decoy_peers(peers,0,3,target,resources,third=True)
    assert result[1][0]['chart_id']=='other-compatible'
    assert result[2][0]['chart_id']=='only-stage-match'


@pytest.mark.parametrize('quote,dose',[
    ('Do not increase lisinopril to 40 mg daily.','40 mg daily'),
    ('Decrease lisinopril from 20 mg daily to 10 mg daily.','20 mg daily')])
def test_corrected_medication_evidence_rejects_negation_and_old_dose(quote,dose):
    from asclepius.ehr_sandbox.terminology import medication_span_supported
    action='decrease' if quote.startswith('Decrease') else 'increase'
    assert not medication_span_supported({'drug':'lisinopril','action':action,'to_dose':dose,'source_span':quote})
    assert medication_span_supported({'drug':'lisinopril','action':'decrease','to_dose':'10 mg daily',
                                      'source_span':'Decrease lisinopril from 20 mg daily to 10 mg daily.'})


@pytest.mark.parametrize('text',['Patient: elderly woman with CKD.','Repeat labs in 7 days before CT.',
    'Follow up in 6 months after CT.','Take 10 mg daily before CT.'])
def test_clinical_demographic_and_timing_text_does_not_trigger_phi(text):
    from asclepius.validation import residual_identifiers
    assert not residual_identifiers(text)


@pytest.mark.parametrize('age,sex,scr,scys,cr,combined',[
    (27,'male',.68,.78,131,128),(27,'male',.9,.8,120,121),(27,'male',.91,.81,118,120),
    (27,'male',1,1,106,96),(27,'female',.68,.78,122,119),(27,'female',.7,.8,121,117),
    (27,'female',.71,.81,119,115),(27,'female',1,1,79,81),(54,'male',.68,.78,110,115),
    (54,'male',.9,.8,101,109),(54,'male',.91,.81,100,108),(54,'male',1,1,89,87),
    (54,'female',.68,.78,103,107),(54,'female',.7,.8,103,105),(54,'female',.71,.81,101,103),
    (54,'female',1,1,67,73)])
def test_ckd_epi_against_published_tufts_implementation_vectors(age,sex,scr,scys,cr,combined):
    """Tufts 8Dec2021 Appendix p6, all16 cases around creatinine/cystatin knots.

    https://www.tuftsmedicine.org/sites/default/files/2023-06/Implementation-of-2021-CKD-EPI-Equations%20-8%20Dec-21.pdf
    Published results are rounded to whole mL/min/1.73m2.
    """
    from asclepius.ehr_sandbox.calculators import calculate
    inputs={'age':age,'sex':sex,'creatinine_mg_dl':scr}
    assert round(calculate('egfr_ckd_epi_2021_cr',inputs)['value'])==cr
    assert round(calculate('egfr_ckd_epi_2021_cr_cys',{**inputs,'cystatin_c_mg_l':scys})['value'])==combined


def test_non_mass_doses_keep_units_and_never_become_renal_mg_doses():
    from asclepius.ehr_sandbox.items import compatible, key_items
    from asclepius.ehr_sandbox.terminology import daily_dose,daily_amount
    assert daily_amount('10 mEq twice daily')=={'value':20,'unit':'mEq'}
    assert daily_dose('10 mEq twice daily') is None
    key=key_items({'med_changes':[{'drug':'potassium chloride','action':'start','to_dose':'20 mEq daily'}]})[0]
    assert not key.get('not_gradable')
    assert compatible(key,{**key,'dose':20,'dose_unit':'mEq'})==1
    assert compatible(key,{**key,'dose':20,'dose_unit':'mg'})==0


def test_outcome_review_judges_one_reference_plan_before_revealing_followup(store,compiled,monkeypatch):
    from asclepius.ehr_sandbox import harness
    task,_=compiled;key=harness.effective_key(store.ehr_get('ehr_visits',visit_id=task['visit_id']),store=store)
    item=__import__('asclepius.ehr_sandbox.items',fromlist=['key_items']).key_items(key)[0]
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a'),candidate('b')])
    row=reviews.create_review(task['visit_id'],'outcome_flag',[{'item_id':item['item_id'],'key':item,'actual':None}],scope='visit',store=store)
    first=store.ehr_all('ehr_review_assignments',review_id=row['review_id'])[0]['user_id']
    payload=reviews.view(row['review_id'],first,store=store)
    assert payload['items'][0]['proposed_plan'] and 'proposed_medications' in payload
    from fastapi import HTTPException
    with pytest.raises(HTTPException): reviews.reveal_outcome(row['review_id'],first,store=store)
    body={'confidence':'high','seconds_spent':90,'items':[{'item_id':item['item_id'],'reference_decision':'inappropriate','rationale':'The available chart does not support this proposed decision.'}]}
    assert reviews.submit(row['review_id'],first,body,store=store)['status']=='needs_second'
    assert 'outcome' in reviews.reveal_outcome(row['review_id'],first,store=store)
    assert not store.ehr_all('ehr_key_corrections')
    second=next(a['user_id'] for a in store.ehr_all('ehr_review_assignments',review_id=row['review_id']) if a['round']==2)
    assert reviews.submit(row['review_id'],second,body,store=store)['status']=='resolved'
    assert len(store.ehr_all('ehr_key_corrections'))==1


def test_dose_probe_checks_answer_without_requiring_treatment_of_baseline_risk(store,compiled):
    from asclepius.ehr_sandbox.grader import grade
    task,_=compiled;task=copy.deepcopy(task);task['task_kind']='probe_dose'
    snapshot=json.loads(task['snapshot_json'])
    for entry in snapshot['entry']:
        r=entry['resource']
        if r['resourceType']=='Observation' and any(c.get('code')=='2823-3' for c in r.get('code',{}).get('coding',[])):
            r['valueQuantity']['value']=7
    task['snapshot_json']=dumps(snapshot)
    key={'answer':{'action':'stop','daily_dose_mg':0}}
    result=grade(task,{'answer':key['answer']},key)
    assert result['reward']==1 and not result['hard_fail']


def test_sampled_dose_reference_waits_for_review_and_rejected_reference_is_withdrawn(store,compiled,monkeypatch):
    from asclepius.ehr_sandbox import harness
    task,_=compiled;probe=copy.deepcopy(task)
    probe.update(task_id='dose-sample-task',task_kind='probe_dose',probe_key_enc=seal({'answer':{'action':'stop','daily_dose_mg':0},'drug':'metformin'}))
    store.ehr_insert('ehr_tasks',probe)
    original_digest=reviews.digest
    monkeypatch.setattr(reviews,'digest',lambda value:'00000000'+'a'*56 if isinstance(value,str) and value.startswith('ehrr') else original_digest(value))
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a'),candidate('b')])
    result=asyncio.run(harness.run(probe['task_id'],model='scripted:oracle',store=store))
    rollout=store.ehr_get('ehr_rollouts',rollout_id=result['rollouts'][0]['rollout_id'])
    row=store.ehr_get('ehr_reviews',rollout_id=rollout['rollout_id'],trigger='dose_sample')
    assert row is not None
    assert rollout['final_reward'] is None
    body={'confidence':'high','seconds_spent':90,'items':[{'item_id':'dose-reference','reference_decision':'inappropriate',
        'rationale':'The reference lacks the indication needed to support this dose.'}]}
    for _ in range(2):
        assignment=next(a for a in store.ehr_all('ehr_review_assignments',review_id=row['review_id']) if a['status']=='offered')
        reviews.submit(row['review_id'],assignment['user_id'],body,store=store)
    assert store.ehr_get('ehr_tasks',task_id=probe['task_id'])['status']=='excluded'
    assert store.ehr_get('ehr_rollouts',rollout_id=rollout['rollout_id'])['status']=='reference_excluded'
    assert not store.ehr_all('ehr_key_corrections')


def test_titration_step_requires_explicit_approved_equivalence(monkeypatch):
    from asclepius.ehr_sandbox import terminology,items
    from pathlib import Path
    original=Path.read_text
    table={'status':'approved','approved_by':['reviewer-a','reviewer-b'],
        'ladders':[{'drug':'synthetic-drug','unit':'mg','source':'synthetic test protocol, not clinical guidance','steps':[[10,15],[20,30]]}]}
    monkeypatch.setattr(Path,'read_text',lambda p,*a,**kw:dumps(table) if p.name=='titration_ladders.json' else original(p,*a,**kw))
    key={'type':'med','ingredient':'synthetic-drug','action':'increase','dose':10,'dose_unit':'mg'}
    assert items.compatible(key,{**key,'dose':15})==1
    assert items.compatible(key,{**key,'dose':20})==0
    assert items.compatible(key,{**key,'dose':15,'action':'decrease'})==0
    table['approved_by']=['reviewer-a']
    assert items.compatible(key,{**key,'dose':15})==0


def test_alternating_outcome_ratings_are_not_straight_line_earnings_hold(store,compiled,monkeypatch):
    task,_=compiled
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a'),candidate('b')])
    items=[{'item_id':f'lab-{i}','key':{'type':'lab','group':'BMP','timing_days':i+1},'actual':None} for i in range(5)]
    row=reviews.create_review(task['visit_id'],'outcome_flag',items,scope='visit',store=store)
    assignment=store.ehr_all('ehr_review_assignments',review_id=row['review_id'])[0]
    from tests.test_ehr_sandbox_workflow import opened_minutes_ago
    opened_minutes_ago(store,row['review_id'])
    reviews.submit(row['review_id'],assignment['user_id'],{'confidence':'high','seconds_spent':90,'items':[
        {'item_id':item['item_id'],'reference_decision':'appropriate' if i%2 else 'inappropriate',
         'rationale':'This decision was independently considered against the available chart.'} for i,item in enumerate(items)]},store=store)
    earning=store.get_earning(kind='ehr_review',ref_id=assignment['review_assignment_id'])
    assert earning['status']=='approved'


def _report_rollout(store,rid,status='final',reward=1,trace=None):
    store.ehr_insert('ehr_rollouts',{'rollout_id':rid,'task_id':'report-task','model':'synthetic-agent','provider':'fake','run_group':'report-group',
        'trajectory_json':dumps(trace or {}),'access_log_json':'[]','writes_json':'[]','created_at':'2031-01-01',
        'status':status,'final_reward':reward,'provisional_reward':1})
    store.ehr_insert('ehr_checkpoints',{'checkpoint_id':'cp-'+rid,'rollout_id':rid,'kind':'act','score':1,'verdict':'pass','items_json':'[]','grader':'code'})


def test_report_separates_pending_and_withdrawn_from_final_scores(store):
    from asclepius.ehr_sandbox.report import build
    _report_rollout(store,'waiting','awaiting_review',None)
    _report_rollout(store,'withdrawn','reference_excluded',None)
    m=build(store=store)['report']['models']['synthetic-agent']
    assert m['pass_at_1'] is None and m['full_success_rate'] is None and m['hard_fail_rate'] is None
    assert (m['pending'],m['withdrawn'],m['n_final'],m['n_provisional'])==(1,1,0,1)
    _report_rollout(store,'final')
    m=build(store=store)['report']['models']['synthetic-agent']
    assert m['pass_at_1']==1 and m['n_final']==1 and m['pass_pow_3'] is None


def test_rubric_agreement_stratifies_version_and_actual_judge(store):
    from asclepius.ehr_sandbox.report import build
    for version in ('v1','v2'):
        for judge in ('model-a','model-b'):
            for i in range(25):
                rid=f'{version}-{judge}-{i}'
                _report_rollout(store,rid,trace={'rubric_enc':seal({'rubric_version':version,'judge_model':judge,'judge_provider':'fake'})})
                store.ehr_insert('ehr_reviews',{'review_id':'review-'+rid,'scope':'rollout','rollout_id':rid,'visit_id':'report-visit',
                    'trigger':'rubric_sample','dedupe_key':rid,'items_json':dumps({'sealed':seal([{'item_id':'criterion','criterion':{'met':True}}])}),
                    'blind_order':'a_is_doctor','status':'resolved','created_at':'2031-01-01',
                    'resolution_json':dumps({'items':{'criterion':'rubric_unmet'}})})
    agreements=list(build(store=store)['report']['reviews']['rubric_agreement'].values())
    assert len(agreements)==4
    assert {(a['rubric_version'],a['judge_model']) for a in agreements}=={(v,m) for v in ('v1','v2') for m in ('model-a','model-b')}
    assert all(a['n']==25 and not a['revision_required'] for a in agreements)


def test_rubric_result_records_judge_identity(store,monkeypatch):
    from asclepius.ehr_sandbox.rubric import judge
    monkeypatch.setenv('MODEL_EHR_RUBRIC_JUDGE','claude-sonnet-4-6')
    result=asyncio.run(judge('synthetic note',[],rubric={'status':'approved','approved_by':['synthetic-1','synthetic-2'],
        'rubric_version':'synthetic-v1','criteria':[{'id':'c1','description':'Synthetic test criterion','points':1}]},store=store))
    assert result['judge_model']=='claude-sonnet-4-6' and result['judge_provider']=='fake'


def test_straight_lined_outcome_ratings_are_held_for_a_person(store,compiled,monkeypatch):
    task,_=compiled
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a'),candidate('b')])
    items=[{'item_id':f'lab-{i}','key':{'type':'lab','group':'BMP','timing_days':i+1},'actual':None} for i in range(5)]
    row=reviews.create_review(task['visit_id'],'outcome_flag',items,scope='visit',store=store)
    assignment=store.ehr_all('ehr_review_assignments',review_id=row['review_id'])[0]
    from tests.test_ehr_sandbox_workflow import opened_minutes_ago
    opened_minutes_ago(store,row['review_id'])
    reviews.submit(row['review_id'],assignment['user_id'],{'confidence':'high','seconds_spent':90,'items':[
        {'item_id':item['item_id'],'reference_decision':'appropriate',
         'rationale':'This decision was independently considered against the available chart.'} for item in items]},store=store)
    earning=store.get_earning(kind='ehr_review',ref_id=assignment['review_assignment_id'])
    assert earning['status']=='accrued' and earning['quality_hold'] and 'straight_lined' in earning['quality_reasons_json']


def test_provider_failure_is_not_scored_or_sent_to_paid_review(store,compiled,monkeypatch):
    from asclepius.ehr_sandbox import harness
    from asclepius.ehr_sandbox.report import build
    task,_=compiled
    async def outage(*a,**kw): raise TimeoutError('provider unavailable')
    monkeypatch.setattr(harness,'drive',outage)
    result=asyncio.run(harness.run(task['task_id'],model='synthetic-provider-model',store=store))
    rollout=store.ehr_get('ehr_rollouts',rollout_id=result['rollouts'][0]['rollout_id'])
    assert rollout['status']=='provider_error' and rollout['final_reward'] is None
    assert not store.ehr_all('ehr_reviews',rollout_id=rollout['rollout_id'])
    m=build(store=store)['report']['models']['synthetic-provider-model']
    assert m['provider_errors']==1 and m['n_provisional']==0 and m['other_incomplete']==0


def test_one_unofferable_review_does_not_stop_the_hourly_sweep(store,compiled,monkeypatch):
    task,_=compiled
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a'),candidate('b')])
    rows=[reviews.create_review(task['visit_id'],'safety',[{'item_id':f's{i}','key':{'type':'lab','group':'BMP'},'actual':None}],scope='visit',store=store)
          for i in range(2)]
    seen=[]
    def select(review_id,*a,**kw):
        seen.append(review_id)
        if review_id==rows[0]['review_id']: raise ValueError('chart is not eligible for review')
        return []
    monkeypatch.setattr(reviews,'select_reviewers',select)
    result=reviews.reassign_expired(store)
    assert set(seen)>={r['review_id'] for r in rows} and result['reoffer_failed']==[rows[0]['review_id']]
