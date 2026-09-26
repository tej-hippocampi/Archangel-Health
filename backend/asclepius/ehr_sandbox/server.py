"""Portable HTTP runtime. Agent and grader factories run in separate processes."""
import base64
import json
import os
import secrets
from pathlib import Path
from threading import RLock
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, ConfigDict, Field
from .env import EhrVisitEnv

class Reset(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task_id: str
    seed: int | None=None
    session_id: str | None=None

class Step(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id: str
    action: dict

class Grade(BaseModel):
    model_config=ConfigDict(extra='forbid')
    task_id: str
    actions: list[dict] = Field(max_length=200)
    seed: int | None=None

def load_tasks(path):
    return {r['task_id']:r for line in Path(path).read_text().splitlines() if line.strip() for r in [json.loads(line)]}

def create_agent_app(tasks=None):
    tasks=tasks if tasks is not None else load_tasks(os.environ['EHR_TASKS_PATH'])
    app=FastAPI(title='Nephrology EHR agent runtime'); sessions={}; lock=RLock()
    def session(sid):
        if sid not in sessions: raise HTTPException(404,'Unknown episode')
        return sessions[sid]
    @app.get('/health')
    def health(): return {'status':'ok','service':'agent','tasks':len(tasks)}
    @app.post('/reset')
    def reset(body: Reset):
        with lock:
            if body.task_id not in tasks: raise HTTPException(404,'Unknown task')
            if body.session_id:
                session(body.session_id).close(); sid=body.session_id
            else:
                if len(sessions)>=int(os.getenv('EHR_MAX_SESSIONS','256')): raise HTTPException(429,'Close an existing episode first')
                sid=secrets.token_urlsafe(24)
            env=EhrVisitEnv(tasks[body.task_id]);observation,info=env.reset(body.seed);sessions[sid]=env
            return {'session_id':sid,'observation':observation,'info':info,'reward':0,'done':False}
    @app.post('/step')
    def step(body: Step):
        with lock:
            obs,reward,terminated,truncated,info=session(body.session_id).step(body.action)
            return {'observation':obs,'reward':reward,'terminated':terminated,'truncated':truncated,'done':terminated or truncated,'info':info}
    @app.get('/state')
    def state(session_id: str):
        with lock: return session(session_id).state()
    @app.delete('/sessions/{session_id}')
    def close(session_id: str):
        with lock: session(session_id).close();sessions.pop(session_id)
        return {'closed':True}
    @app.get('/fhir/{kind}')
    def search(kind: str,session_id: str,query: str='{}'):
        # Facade reads spend the same episode budget and are replayable by grading.
        try: params=json.loads(query)
        except ValueError: raise HTTPException(400,'query must be a JSON object')
        if not isinstance(params,dict): raise HTTPException(400,'query must be an object')
        mapping={'Patient':'search_patients','Observation':'search_observations','MedicationRequest':'search_medications',
                 'Condition':'search_conditions','Encounter':'list_encounters','AllergyIntolerance':'search_allergies','DocumentReference':'search_documents'}
        if kind not in mapping: raise HTTPException(400,'Use search_orders for service requests and appointments')
        with lock: return session(session_id).step({'tool':mapping[kind],'input':params})[0]
    return app

def decrypt_keys(path,key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    payload=json.loads(Path(path).read_text())
    return json.loads(AESGCM(base64.b64decode(key,validate=True)).decrypt(base64.b64decode(payload['nonce']),
        base64.b64decode(payload['ciphertext']),payload['package_id'].encode()))

def create_grader_app(tasks=None,keys=None,token=None):
    from .grader import grade
    tasks=tasks if tasks is not None else load_tasks(os.environ['EHR_TASKS_PATH'])
    if keys is None: keys=decrypt_keys(os.environ['EHR_KEYS_PATH'],Path(os.environ['EHR_GRADER_KEY_FILE']).read_text().strip())
    token=token if token is not None else os.environ.get('EHR_GRADER_TOKEN')
    if not token: raise ValueError('EHR_GRADER_TOKEN is required')
    app=FastAPI(title='Nephrology EHR private grader')
    @app.get('/health')
    def health(): return {'status':'ok','service':'grader'}
    @app.post('/grade')
    def score(body: Grade,authorization: str=Header(default='')):
        if not secrets.compare_digest(authorization.encode(),('Bearer '+token).encode()): raise HTTPException(403,'Grader authentication required')
        if body.task_id not in tasks or body.task_id not in keys: raise HTTPException(404,'Unknown task')
        env=EhrVisitEnv(tasks[body.task_id]);env.reset(body.seed)
        # Never trust client-supplied access logs, writes or calculated values.
        for action in body.actions: env.step(action)
        record=keys[body.task_id];key=record.get('key',record);policy=record.get('policy',{})
        result=grade(tasks[body.task_id],env.rollout(),key)
        if policy:
            from .review_policy import overrides
            result=grade(tasks[body.task_id],env.rollout(),key,review_overrides=overrides(result,policy))
        return {'reward':result['reward'],'hard_fail':result['hard_fail'],'failure_tags':result['failure_tags'],
                'document_mode':result.get('document_mode'),'checkpoints':[{k:c[k] for k in ('kind','score','verdict','grader')} for c in result['checkpoints']],
                'step_rewards':result.get('step_rewards',[])}
    return app
