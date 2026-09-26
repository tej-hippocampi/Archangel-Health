"""Real-store compilation, routing, independent review and one ledger row on retry."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import pytest
from fastapi import HTTPException
from asclepius.store import reset_store_for_tests
from asclepius.ehr_sandbox import chart_builder,visit_compiler,reviews,harness
from asclepius.ehr_sandbox.common import dumps,identifier

@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_ENCRYPTION_KEY',base64.b64encode(b'z'*32).decode())
    monkeypatch.setenv('EHR_RUBRIC_SAMPLE_RATE','0')
    return reset_store_for_tests(str(tmp_path/'workflow.db'))

@pytest.fixture
def compiled(store,monkeypatch):
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[])
    import asclepius.store as store_module
    import itertools
    counter=itertools.count(); original_id=store_module._new_id
    monkeypatch.setattr(store_module,'_new_id',lambda prefix: f'icase-workflow-{next(counter)}' if prefix=='icase' else original_id(prefix))
    original=json.loads((Path(__file__).parent/'fixtures/ehr_sandbox/cases/p01.json').read_text())
    original['notes']=original['notes'][1:]
    original['notes'][0]['text']='CKD stage 4 (N18.4). Follow up in 28 days.'
    original['notes'][1]['text']='Hypertension (I10). Decrease Prinivil to 10 mg once daily. Check BMP in 7 days. Follow up in 42 days.'
    original['orders']=[]; original['medication_events']=[]
    charts=[]
    for i in range(12):
        case=copy.deepcopy(original); case['case_id']=f'workflow-{i}'
        row=store.insert_ingest_case(upload_id='workflow-upload',patient_key=case['case_id'],specialty='nephrology',case=case,status='ingested',report={})
        charts.append(asyncio.run(chart_builder.build_chart(row['ingest_case_id'],store=store)))
    by_split={split:[c for c in charts if visit_compiler.split_for(c['chart_id'])==split] for split in ('train','dev','heldout')}
    chart=next(v[0] for v in by_split.values() if len(v)>=3)
    visit_compiler.compile_chart(chart['chart_id'],store=store)
    for visit in store.ehr_list('ehr_visits',chart_id=chart['chart_id']):
        visit_compiler.waive_audit(visit['visit_id'],'Synthetic integration fixture key independently checked','test',store=store)
    result=visit_compiler.compile_chart(chart['chart_id'],store=store)
    assert len(result['built'])==2,result
    assert result['balance']['balanced']
    task=store.ehr_get('ehr_tasks',task_id=result['built'][1]['task_id'])
    return task,chart


def test_compiler_audits_decoys_balance_and_probes(store,compiled):
    task,chart=compiled;bundle=json.loads(task['snapshot_json'])
    assert len([e for e in bundle['entry'] if e['resource']['resourceType']=='Patient'])==3
    assert len(store.ehr_list('ehr_reviews',trigger='key_audit'))==2
    assert any(t['task_kind']=='probe_retrieval' for t in store.ehr_list('ehr_tasks',visit_id=task['visit_id']))
    before=task['snapshot_json'];visit_compiler.compile_chart(chart['chart_id'],store=store)
    assert store.ehr_get('ehr_tasks',task_id=task['task_id'])['snapshot_json']==before


def test_harness_persists_and_groups_disagreement(store,compiled):
    task,_=compiled
    result=asyncio.run(harness.run(task['task_id'],model='scripted:planted',k=2,store=store))
    assert len(result['rollouts'])==2
    assert len(store.ehr_list('ehr_rollouts',run_group=result['run_group']))==2
    assert len(store.ehr_list('ehr_reviews',trigger='disagreement'))==2
    assert all(r['status']=='awaiting_review' for r in store.ehr_list('ehr_rollouts',run_group=result['run_group']))


def candidate(uid): return {'user':{'id':uid,'email':''},'domain_match':1}

def verdict(row,confidence='high',a='appropriate',b='appropriate'):
    return {'confidence':confidence,'seconds_spent':90,'items':[{'item_id':i['item_id'],'plan_a':a,'plan_b':b,'better':'equivalent',
             **({'safety_decision':'confirmed'} if row['trigger']=='safety' else {}),'rationale':'The structured plan is supported by the available chart evidence.'} for i in reviews.review_items(row)]}


def offered_minutes_ago(store,review_id,minutes=10):
    """Reviews are paid on the server's clock since the offer, not the browser's timer."""
    from datetime import datetime,timedelta,timezone
    when=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).replace(tzinfo=None).isoformat(timespec='seconds')
    with store._conn() as conn: conn.execute('UPDATE ehr_review_assignments SET offered_at=? WHERE review_id=?',(when,review_id))


def test_assignment_gate_ledger_retry_and_resolution(store,compiled,monkeypatch):
    task,_=compiled;monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('reviewer-1')])
    row=reviews.create_review(task['visit_id'],'disagreement',[{'item_id':'x','key':{'type':'lab','group':'BMP'},'actual':{'type':'lab','group':'renal'}}],rollout_id='test-rollout',store=store)
    # No parent rollout for this standalone review; don't create an invalid link.
    with store._conn() as conn: conn.execute("UPDATE ehr_review_rollouts SET rollout_id='unrelated' WHERE review_id=?",(row['review_id'],))
    monkeypatch.setattr(reviews,'recompute_rollout',lambda *a,**kw:None)
    with pytest.raises(HTTPException): reviews.view(row['review_id'],'admin',store=store)
    payload=reviews.view(row['review_id'],'reviewer-1',store=store)
    assert 'blind_order' not in payload and 'source_span' not in dumps(payload)
    body=verdict(row); offered_minutes_ago(store,row['review_id'])
    first=reviews.submit(row['review_id'],'reviewer-1',body,store=store)
    second=reviews.submit(row['review_id'],'reviewer-1',body,store=store)
    assert first['status']=='resolved' and second['already_submitted']
    assignment=store.ehr_get('ehr_review_assignments',review_id=row['review_id'],user_id='reviewer-1')
    earning=store.get_earning(kind='ehr_review',ref_id=assignment['review_assignment_id'])
    assert earning['amount_cents']==2500 and earning['status']=='approved'



def test_instant_submission_is_held_not_paid_whatever_the_browser_timer_says(store,compiled,monkeypatch):
    task,_=compiled;monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('reviewer-1')])
    monkeypatch.setattr(reviews,'recompute_rollout',lambda *a,**kw:None)
    row=reviews.create_review(task['visit_id'],'disagreement',[{'item_id':'x','key':{'type':'lab','group':'BMP'},'actual':{'type':'lab','group':'renal'}}],scope='visit',store=store)
    reviews.submit(row['review_id'],'reviewer-1',verdict(row),store=store)
    assignment=store.ehr_get('ehr_review_assignments',review_id=row['review_id'],user_id='reviewer-1')
    earning=store.get_earning(kind='ehr_review',ref_id=assignment['review_assignment_id'])
    assert earning['status']=='accrued' and earning['quality_hold']
    from datetime import datetime,timedelta,timezone
    later=(datetime.now(timezone.utc)+timedelta(days=90)).replace(tzinfo=None).isoformat(timespec='seconds')
    assert earning['earning_id'] not in {e['earning_id'] for e in store.accrued_earnings_before(later)}


def test_equity_only_reviewer_records_a_verdict_without_accruing_cash(store,compiled,monkeypatch):
    task,_=compiled;monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('reviewer-1')])
    monkeypatch.setattr(reviews,'recompute_rollout',lambda *a,**kw:None)
    monkeypatch.setattr(store,'get_user_by_id',lambda uid:{'id':uid,'compensation_model':'equity_only'})
    row=reviews.create_review(task['visit_id'],'disagreement',[{'item_id':'x','key':{'type':'lab','group':'BMP'},'actual':{'type':'lab','group':'renal'}}],scope='visit',store=store)
    offered_minutes_ago(store,row['review_id'])
    reviews.submit(row['review_id'],'reviewer-1',verdict(row),store=store)
    assignment=store.ehr_get('ehr_review_assignments',review_id=row['review_id'],user_id='reviewer-1')
    assert store.get_earning(kind='ehr_review',ref_id=assignment['review_assignment_id']) is None
    assert store.ehr_get('ehr_review_verdicts',review_id=row['review_id'],reviewer_user_id='reviewer-1')

def test_independent_second_reviewer_and_source_exclusion(store,compiled,monkeypatch):
    task,chart=compiled;monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a'),candidate('b'),candidate('excluded')])
    store.ehr_insert('ehr_source_exclusions',{'upload_id':chart['upload_id'],'user_id':'excluded','reason':'source_practice_clinician'})
    row=reviews.create_review(task['visit_id'],'safety',[{'item_id':'s','key':{'type':'lab','group':'BMP'},'actual':None}],scope='visit',store=store)
    first=store.ehr_list('ehr_review_assignments',review_id=row['review_id'])[0]
    reviews.submit(row['review_id'],first['user_id'],verdict(row,'low'),store=store)
    assignments=store.ehr_list('ehr_review_assignments',review_id=row['review_id'])
    assert len(assignments)==2 and len({a['user_id'] for a in assignments})==2
    assert all(a['user_id']!='excluded' for a in assignments)
    second=next(a for a in assignments if a['round']==2)
    reviews.submit(row['review_id'],second['user_id'],verdict(row),store=store)
    assert store.ehr_get('ehr_reviews',review_id=row['review_id'])['status']=='resolved'
