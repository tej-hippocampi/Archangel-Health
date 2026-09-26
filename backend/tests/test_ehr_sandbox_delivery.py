"""Portable package boundary, replay grading and realm-gated HTTP control plane."""
import asyncio
import base64
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_ehr_sandbox_workflow import store,compiled,candidate,verdict
from asclepius.ehr_sandbox import package,harness,reviews
from asclepius.ehr_sandbox.server import create_agent_app,create_grader_app,decrypt_keys

ATTEST={'buyer_jurisdiction':'GB','buyer_not_covered_person':True,'onward_transfer_clause':True}


def test_package_attestations_and_runtime_boundary(store,compiled,tmp_path):
    task,_=compiled
    with pytest.raises(ValueError,match='Only train'): package.build('heldout',ATTEST,'buyer',store=store,output_root=tmp_path)
    for change in ({'buyer_jurisdiction':'CN'},{'buyer_jurisdiction':''},{'buyer_not_covered_person':False},{'onward_transfer_clause':False}):
        with pytest.raises(ValueError): package.build('dev',{**ATTEST,**change},'buyer',store=store,output_root=tmp_path)
    assert task['split']!='heldout'
    result=package.build(task['split'],ATTEST,'synthetic-test-buyer',store=store,output_root=tmp_path)
    root=Path(result['directory_path']);key=Path(result['key_path']).read_text().strip()
    assert not (root/'server/ehr_runtime/grader.py').exists()
    assert not any(p.suffix=='.key' for p in root.rglob('*'))
    assert not (root/'server/ehr_runtime/harness.py').exists()
    rows=[json.loads(line) for line in (root/'tasks'/f"{task['split']}.jsonl").read_text().splitlines()]
    assert rows and all('key_enc' not in r and 'visit_id' not in r and r['canary']==result['canary'] for r in rows)
    keys=decrypt_keys(root/'graders/keys.enc',key)
    assert keys[task['task_id']]['key']==harness.effective_key(store.ehr_get('ehr_visits',visit_id=task['visit_id']),store=store)
    manifest=json.loads((root/'manifest.json').read_text())
    import hashlib
    assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==value for name,value in manifest['files'].items())
    with zipfile.ZipFile(result['archive_path']) as archive:
        assert not any(name.endswith('.key') for name in archive.namelist())
    # Import/run the actual vendored agent with no application on PYTHONPATH.
    script='''import json
from fastapi.testclient import TestClient
from ehr_runtime.server import create_agent_app
client=TestClient(create_agent_app())
row=json.loads(open(__import__('os').environ['EHR_TASKS_PATH']).readline())
r=client.post('/reset',json={'task_id':row['task_id']});assert r.status_code==200,r.text
sid=r.json()['session_id'];assert 'snapshot' not in r.json()['observation']
assert client.post('/grade',json={}).status_code==404
for _ in range(5): assert client.post('/step',json={'session_id':sid,'action':{'tool':'search_patients','input':{}}}).status_code==200
assert client.post('/step',json={'session_id':sid,'action':{'tool':'finish_visit','input':{}}}).json()['done']
'''
    env={**os.environ,'PYTHONPATH':str(root/'server'),'EHR_TASKS_PATH':str(root/'tasks'/f"{task['split']}.jsonl")}
    run=subprocess.run([sys.executable,'-c',script],env=env,cwd=tmp_path,capture_output=True,text=True)
    assert run.returncode==0,run.stderr


def test_grader_requires_token_and_replays_actions(store,compiled):
    task,_=compiled;key=harness.effective_key(store.ehr_get('ehr_visits',visit_id=task['visit_id']),store=store)
    agent=TestClient(create_agent_app({task['task_id']:task}))
    grader=TestClient(create_grader_app({task['task_id']:task},{task['task_id']:key},'secret'))
    assert agent.post('/grade',json={}).status_code==404
    assert grader.post('/reset',json={'task_id':task['task_id']}).status_code==404
    body={'task_id':task['task_id'],'actions':[{'tool':'finish_visit','input':{}}]}
    assert grader.post('/grade',json=body).status_code==403
    result=grader.post('/grade',json=body,headers={'Authorization':'Bearer secret'})
    assert result.status_code==200,result.text
    assert result.json()['reward']<=.15
    assert grader.post('/grade',json={**body,'overlay':[{}]},headers={'Authorization':'Bearer secret'}).status_code==422


def test_router_auth_and_key_safe_lists(store,compiled,monkeypatch):
    from routers import asclepius_ehr_sandbox as routes
    app=FastAPI();app.include_router(routes.router)
    app.dependency_overrides[routes.require_admin]=lambda:{'id':'admin'}
    app.dependency_overrides[routes.require_current_agreement]=lambda:{'id':'nonowner'}
    monkeypatch.setattr(routes,'get_store',lambda:store)
    client=TestClient(app)
    for path in ('/charts','/tasks','/summary','/admin/reviews'):
        result=client.get('/api/asclepius/ehr-sandbox'+path);assert result.status_code==200,result.text
        assert 'key_enc' not in result.text and 'source_span' not in result.text
    review=store.ehr_all('ehr_reviews')[0]
    assert client.get('/api/asclepius/ehr-sandbox/reviews/'+review['review_id']).status_code==403
    assert client.get('/api/asclepius/ehr-sandbox/reviews/queue').json()=={'assignments':[]}


def test_same_dispute_resolution_updates_other_rollouts(store,compiled,monkeypatch):
    task,_=compiled
    result=asyncio.run(harness.run(task['task_id'],model='scripted:planted',k=2,store=store))
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a')])
    rows=store.ehr_all('ehr_reviews',trigger='disagreement');reviews.select_reviewers(rows[0]['review_id'],store=store)
    reviews.submit(rows[0]['review_id'],'a',verdict(rows[0]),store=store)
    assert all(r['status']=='resolved' for r in store.ehr_all('ehr_reviews',trigger='disagreement'))
    assert all(r['status']=='final' for r in store.ehr_all('ehr_rollouts',run_group=result['run_group']))


def test_critical_harm_changes_reward_and_preserves_rubric(store,compiled,monkeypatch):
    from asclepius.ehr_sandbox import rubric
    async def zero(*a,**k): return {'score':0,'mode':'rubric_llm','criteria':[],'rubric_version':'test'}
    monkeypatch.setattr(rubric,'judge',zero)
    task,_=compiled
    result=asyncio.run(harness.run(task['task_id'],model='scripted:oracle',store=store))
    row=store.ehr_get('ehr_rollouts',rollout_id=result['rollouts'][0]['rollout_id'])
    assert row['final_reward']==row['provisional_reward']
    assert row['final_reward']<1
    planted=asyncio.run(harness.run(task['task_id'],model='scripted:planted',store=store))
    rid=planted['rollouts'][0]['rollout_id'];review=store.ehr_get('ehr_reviews',rollout_id=rid,trigger='disagreement')
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a'),candidate('b')]);reviews.select_reviewers(review['review_id'],store=store)
    for _ in range(2):
        assignment=next(a for a in store.ehr_all('ehr_review_assignments',review_id=review['review_id']) if a['status']=='offered')
        body=verdict(review,a='harmful' if review['blind_order']=='a_is_agent' else 'appropriate',b='harmful' if review['blind_order']=='a_is_doctor' else 'appropriate')
        for item in body['items']: item['critical']=True
        reviews.submit(review['review_id'],assignment['user_id'],body,store=store)
    final=store.ehr_get('ehr_rollouts',rollout_id=rid)
    assert final['final_reward']==0 and final['hard_fail']==1


def test_signature_preserves_diagnosis_meaning():
    a={'key':{'type':'assessment','icd10':'N18.4','text':'CKD'},'actual':{'type':'assessment','icd10':'N18.2','text':'CKD'}}
    b={**a,'actual':{**a['actual'],'icd10':'N18.6'}}
    assert reviews.signature(a)!=reviews.signature(b)
    assert reviews.render_plan_item(a['actual'])!=reviews.render_plan_item(b['actual'])


def test_export_preserves_physician_alternative_grade(store,compiled,monkeypatch,tmp_path):
    task,_=compiled
    result=asyncio.run(harness.run(task['task_id'],model='scripted:planted',store=store));rid=result['rollouts'][0]['rollout_id']
    review=store.ehr_get('ehr_reviews',rollout_id=rid,trigger='disagreement')
    monkeypatch.setattr(reviews,'load_candidates',lambda s:[candidate('a')]);reviews.select_reviewers(review['review_id'],store=store)
    reviews.submit(review['review_id'],'a',verdict(review),store=store)
    final=store.ehr_get('ehr_rollouts',rollout_id=rid)
    result=package.build(task['split'],ATTEST,'policy-test',store=store,output_root=tmp_path)
    root=Path(result['directory_path']);keys=decrypt_keys(root/'graders/keys.enc',Path(result['key_path']).read_text().strip())
    client=TestClient(create_grader_app({task['task_id']:task},keys,'test'))
    actions=[{'tool':t['tool'],'input':t['input']} for t in json.loads(final['trajectory_json'])['trajectory'] if t['type']=='tool_call']
    response=client.post('/grade',json={'task_id':task['task_id'],'actions':actions},headers={'Authorization':'Bearer test'})
    assert response.status_code==200,response.text
    assert response.json()['reward']==final['final_reward']
    manifest=json.loads((root/'manifest.json').read_text())
    assert manifest['source_countries']==['US'] and manifest['attestations']==ATTEST and manifest['buyer_ref']=='policy-test'
