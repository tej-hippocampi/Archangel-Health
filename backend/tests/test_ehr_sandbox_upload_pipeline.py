"""Synthetic files through the real upload, chart, audit, compile and rollout APIs.
No canonical case or task is patched after intake. Model/reviewer fixtures are
software test doubles, not evidence of clinical approval.
"""
import base64
import json
from collections import Counter
from pathlib import Path
import pytest
from tests import _asclepius as A
from tests import test_asclepius_ingestion as I
from tests.test_ehr_sandbox_workflow import candidate,verdict
from asclepius.ehr_sandbox import reviews,harness,visit_compiler

PREFIX='/api/asclepius/ehr-sandbox'
ROOT=Path(__file__).parent/'fixtures/ehr_sandbox'

@pytest.fixture
def uploaded(monkeypatch,tmp_path):
    store=A.fresh_store()
    for key,value in {'ASCLEPIUS_INGEST_DIR':str(tmp_path/'ingest'),'ASCLEPIUS_EXPORT_DIR':str(tmp_path/'exports'),
                      'DATA_ENCRYPTION_KEY':base64.b64encode(b'z'*32).decode(),'EHR_OCR_ENABLED':'0','EHR_RUBRIC_SAMPLE_RATE':'0'}.items():
        monkeypatch.setenv(key,value)
    # Stable fixture identifiers make cohort splits repeatable, without altering
    # any parsed clinical content or compiler result.
    import itertools
    import asclepius.store as sm
    counter=itertools.count();original=sm._new_id
    monkeypatch.setattr(sm,'_new_id',lambda prefix:f'icase-upload-{next(counter)}' if prefix=='icase' else original(prefix))
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('synthetic-reviewer')])
    archive=(ROOT/'qualification_patients.zip').read_bytes();admin=I._admin_h();link=I._mint(admin)
    upload=I._upload(link['token'],archive);uid=upload['upload_id']
    cases=store.list_ingest_cases(upload_id=uid,limit=1000)
    assert len(cases)==48
    assert all(c['status']=='ingested' for c in cases),[(c['patient_key'],c['status'],c.get('report')) for c in cases if c['status']!='ingested']
    originals={c['ingest_case_id']:c['case'] for c in cases}
    result=I.client.post(PREFIX+'/charts/build',headers=admin,json={'upload_id':uid})
    assert result.status_code==202,result.text
    charts=store.ehr_all('ehr_charts',upload_id=uid)
    assert len(charts)==48 and all(c['status']=='built' and c['n_visits']==4 for c in charts),[(c['status'],c.get('extraction_json')) for c in charts]
    for chart in charts:
        response=I.client.post(PREFIX+f"/charts/{chart['chart_id']}/compile",headers=admin,json={})
        assert response.status_code==200,response.text
    for review in store.ehr_all('ehr_reviews',trigger='key_audit'):
        reviews.select_reviewers(review['review_id'],store=store)
        reviews.view(review['review_id'],'synthetic-reviewer',store=store)
        result=reviews.submit(review['review_id'],'synthetic-reviewer',{'confidence':'high','seconds_spent':90,'items':[],
            'key_audit':{'status':'correct','rationale':'Hand-authored synthetic worksheet and extracted decisions agree.'}},store=store)
        assert result['status']=='resolved'
    results=[]
    for chart in charts:
        response=I.client.post(PREFIX+f"/charts/{chart['chart_id']}/compile",headers=admin,json={})
        assert response.status_code==200,response.text
        results.append(response.json())
    assert all(store.get_ingest_case(cid)['case']==case for cid,case in originals.items())
    return store,admin,charts,results


def test_upload_to_episodes(uploaded):
    store,admin,charts,results=uploaded
    summary=Counter(x['reason'] for r in results for x in r['excluded']+r['pending'])
    visits=store.ehr_all('ehr_tasks',task_kind='visit',status='ready')
    print('PIPELINE',len(visits),dict(summary))
    assert visits and any(t['split']=='train' for t in visits)
    assert not summary.get('worksheet_text_overlap')
    assert not summary.get('baseline_sanity_failed'),dict(summary)
    # Choose one action episode from each input family, with prior documents.
    by_chart={c['chart_id']:c for c in charts}
    selected=[]
    for pdf in (False,True):
        for task in visits:
            v=store.ehr_get('ehr_visits',visit_id=task['visit_id']);c=by_chart[v['chart_id']]
            original=store.get_ingest_case(c['ingest_case_id'])
            is_pdf=not original['case'].get('medications')
            if is_pdf==pdf and task['split']=='train' and 'action' in json.loads(task['tags_json']) and v['visit_index']>0:
                selected.append(task);break
    assert len(selected)==2
    groups=[]
    for model in ('scripted:noop','scripted:oracle','scripted:planted'):
        response=I.client.post(PREFIX+'/runs',headers=admin,json={'task_ids':[t['task_id'] for t in selected],'model':model,'k':2})
        assert response.status_code==202,response.text
        group=response.json()['run_group'];groups.append(group)
        status=I.client.get(PREFIX+'/runs/'+group,headers=admin).json()
        assert any(e['event_type']=='completed' for e in status['events']),status
        rollouts=store.ehr_all('ehr_rollouts',run_group=group)
        assert len(rollouts)==4
        for row in rollouts:
            assert row['terminated_by']=='finish_visit'
            assert not json.loads(row['rejected_writes_json'])
            if model=='scripted:oracle':assert row['provisional_reward']>=.9
            elif model=='scripted:noop':assert row['provisional_reward']<=.15
        assert 'key_enc' not in I.client.get(PREFIX+'/reports/'+group,headers=admin).text
    # Resolve synthetic disagreements through the real assignment/verdict path.
    for review in store.ehr_all('ehr_reviews'):
        review=store.ehr_get('ehr_reviews',review_id=review['review_id'])
        if review['status'] in ('resolved','cancelled'):continue
        assert review['trigger']=='disagreement',review['trigger']
        reviews.select_reviewers(review['review_id'],store=store)
        reviews.view(review['review_id'],'synthetic-reviewer',store=store)
        body=verdict(review,a='inappropriate' if review['blind_order']=='a_is_agent' else 'appropriate',
                     b='inappropriate' if review['blind_order']=='a_is_doctor' else 'appropriate')
        result=reviews.submit(review['review_id'],'synthetic-reviewer',body,store=store)
        assert result['status']=='resolved'
        retry=reviews.submit(review['review_id'],'synthetic-reviewer',body,store=store)
        assert retry['already_submitted']
    for group in groups:
        rows=store.ehr_all('ehr_rollouts',run_group=group)
        assert all(r['status']=='final' and r['final_reward'] is not None for r in rows)
    # Public HTTP export, separate encrypted-key download, then action replay.
    import io,zipfile,os
    from fastapi.testclient import TestClient
    from asclepius.ehr_sandbox.server import create_agent_app,create_grader_app,decrypt_keys
    from asclepius.ehr_sandbox import package
    response=I.client.post(PREFIX+'/exports/package',headers=admin,json={'split':'train','buyer_jurisdiction':'US',
        'buyer_not_covered_person':True,'onward_transfer_clause':True,'buyer_ref':'synthetic-qualification'})
    assert response.status_code==200,response.text
    exported=response.json();eid=exported['export_id']
    archive=I.client.get(PREFIX+f'/exports/{eid}/archive',headers=admin)
    assert archive.status_code==200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert not any(n.endswith('.key') or 'harness.py' in n for n in z.namelist())
        public=[json.loads(line) for line in z.read('tasks/train.jsonl').splitlines()]
    assert all(not any(k.endswith('_enc') for k in t) for t in public)
    path=Path(package.artifact_path(eid,'archive',store=store)).with_suffix('')
    keys=decrypt_keys(path/'graders/keys.enc',Path(package.artifact_path(eid,'grader-key',store=store)).read_text().strip())
    taskmap={t['task_id']:t for t in public}
    agent=TestClient(create_agent_app(taskmap));grader=TestClient(create_grader_app(taskmap,keys,'synthetic-replay'))
    assert agent.post('/grade',json={}).status_code==404
    assert grader.post('/reset',json={'task_id':selected[0]['task_id']}).status_code==404
    for row in store.ehr_all('ehr_rollouts',run_group=groups[1]):
        task_id=row['task_id'];trace=json.loads(row['trajectory_json'])['trajectory']
        actions=[{'tool':t['tool'],'input':t['input']} for t in trace if t['type']=='tool_call']
        result=grader.post('/grade',headers={'Authorization':'Bearer synthetic-replay'},json={'task_id':task_id,'actions':actions})
        assert result.status_code==200,result.text
        assert result.json()['reward']==row['final_reward']
        first=agent.post('/reset',json={'task_id':task_id}).json();sid=first['session_id']
        for action in actions:
            step=agent.post('/step',json={'session_id':sid,'action':action})
            assert step.status_code==200,step.text
        assert step.json()['done']
        again=agent.post('/reset',json={'task_id':task_id}).json()
        assert again['observation']==first['observation']
    if os.getenv('EHR_SMOKE_TASKS_OUT'):
        Path(os.environ['EHR_SMOKE_TASKS_OUT']).write_text(json.dumps({'synthetic_only':True,'tasks':[taskmap[t['task_id']] for t in selected]}))
    print('QUALIFIED',json.dumps({'patients':48,'visits':192,'ready_visits':len(visits),'excluded':dict(summary),
                                'rollouts':12,'exported_tasks':exported['n_tasks'],'formats':['CCD/XML','PDF+CSV']}))
