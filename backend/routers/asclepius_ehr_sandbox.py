"""Realm-scoped EHR control plane. Keys never appear on admin list endpoints."""
import json
import uuid
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
import realm
from asclepius.auth import require_admin
from asclepius.store import get_store
from routers.asclepius import require_current_agreement
from asclepius.ehr_sandbox import chart_builder, visit_compiler, harness, reviews, report, package
from asclepius.ehr_sandbox.constants import audit_now, settings

router = APIRouter(prefix='/api/asclepius/ehr-sandbox', tags=['ehr-sandbox'])

class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')

class BuildBody(Body):
    upload_id: str = Field(min_length=1, max_length=200)

class CompileBody(Body):
    dry_run: bool = False

class RunBody(Body):
    task_ids: list[str] = Field(default_factory=list, max_length=500)
    split: Literal['train','dev','heldout'] | None = None
    model: str = Field(min_length=1, max_length=120)
    k: int = Field(default=1, ge=1, le=20)
    harness: Literal['native_tools','json_protocol'] = 'native_tools'

class ExclusionBody(Body):
    upload_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    reason: Literal['source_practice_clinician','family','other']

class ExportBody(Body):
    split: Literal['train','dev']
    buyer_jurisdiction: str = Field(min_length=2,max_length=2)
    buyer_not_covered_person: bool
    onward_transfer_clause: bool
    buyer_ref: str = Field(min_length=1,max_length=200)

class WaiveBody(Body):
    reason: str = Field(min_length=20,max_length=2000)

def safe_call(fn, *args, **kwargs):
    try: return fn(*args, **kwargs)
    except ValueError as exc: raise HTTPException(400,str(exc)) from exc

def public_row(row):
    return {k:v for k,v in row.items() if not k.endswith('_enc') and k not in
            ('resources_json','extraction_json','snapshot_json','items_json','blind_order','resolution_json')}

async def job(job_id, realm_name, operation, args):
    with realm.scoped(realm_name):
        store=get_store()
        store.log_event(entity_type='ehr_job',entity_id=job_id,event_type='running',payload={})
        try:
            results=[]
            if operation=='build':
                # A SQL id-only read avoids silently truncating large uploads.
                with store._conn() as conn:
                    ids=[r[0] for r in conn.execute('SELECT ingest_case_id FROM ingest_cases WHERE upload_id=? AND status=? ORDER BY ingest_case_id',(args['upload_id'],'ingested'))]
                for case_id in ids:
                    row=await chart_builder.build_chart(case_id,store=store)
                    results.append({'chart_id':row.get('chart_id'),'status':row.get('status')})
            else:
                for task_id in args['task_ids']:
                    value=await harness.run(task_id,model=args['model'],k=args['k'],harness=args['harness'],store=store,run_group=job_id)
                    results.append({'task_id':task_id,'rollout_ids':[r['rollout_id'] for r in value['rollouts']]})
            store.log_event(entity_type='ehr_job',entity_id=job_id,event_type='completed',payload={'results':results})
        except Exception as exc:
            # Exception messages can contain source text/provider payloads.
            store.log_event(entity_type='ehr_job',entity_id=job_id,event_type='failed',payload={'error_type':type(exc).__name__})

def queue_job(background, operation, args, actor):
    job_id='ehrjob-'+uuid.uuid4().hex
    get_store().log_event(entity_type='ehr_job',entity_id=job_id,event_type='queued',actor=actor,payload={'operation':operation})
    background.add_task(job,job_id,realm.current(),operation,args)
    return {'job_id':job_id,'run_group':job_id,'status':'queued'}

@router.post('/charts/build',status_code=202)
def build_charts(body: BuildBody, background: BackgroundTasks, admin=Depends(require_admin)):
    if not get_store().list_ingest_cases(upload_id=body.upload_id,status='ingested',limit=1):
        raise HTTPException(400,'Upload has no ingested cases. Resolve ingestion holds first.')
    return queue_job(background,'build',body.model_dump(),admin['id'])

@router.get('/charts')
def charts(upload_id: str | None=None, offset: int=Query(0,ge=0), admin=Depends(require_admin)):
    store=get_store(); rows=store.ehr_list('ehr_charts',limit=100,offset=offset,**({'upload_id':upload_id} if upload_id else {}))
    return {'charts':[{**public_row(r),'visits':[public_row(v) for v in store.ehr_all('ehr_visits',chart_id=r['chart_id'])]} for r in rows],
            'next_offset':offset+100 if len(rows)==100 else None}

@router.post('/charts/{chart_id}/compile')
def compile_chart(chart_id: str, body: CompileBody, admin=Depends(require_admin)):
    return safe_call(visit_compiler.compile_chart,chart_id,dry_run=body.dry_run,store=get_store())

@router.post('/visits/{visit_id}/waive-audit')
def waive(visit_id: str,body: WaiveBody,admin=Depends(require_admin)):
    if not get_store().ehr_get('ehr_visits',visit_id=visit_id): raise HTTPException(404,'Visit not found')
    safe_call(visit_compiler.waive_audit,visit_id,body.reason,admin['id'],store=get_store())
    return {'status':'waived'}

@router.get('/tasks')
def tasks(split: Literal['train','dev','heldout'] | None=None,status: Literal['ready','retired'] | None=None,
          offset: int=Query(0,ge=0),admin=Depends(require_admin)):
    filters={k:v for k,v in {'split':split,'status':status}.items() if v}
    rows=get_store().ehr_list('ehr_tasks',limit=100,offset=offset,**filters)
    return {'tasks':[public_row(r) for r in rows],'next_offset':offset+100 if len(rows)==100 else None}

@router.post('/tasks/{task_id}/sanity')
def sanity(task_id: str,admin=Depends(require_admin)):
    store=get_store();task=store.ehr_get('ehr_tasks',task_id=task_id)
    if not task: raise HTTPException(404,'Task not found')
    from asclepius.ehr_sandbox.sealed import unseal
    key=unseal(task['probe_key_enc']) if task.get('probe_key_enc') else harness.effective_key(store.ehr_get('ehr_visits',visit_id=task['visit_id']),store=store)
    return harness.public_verification(harness.sanity(task,key))

@router.post('/runs',status_code=202)
def start_runs(body: RunBody,background: BackgroundTasks,admin=Depends(require_admin)):
    if bool(body.task_ids)==bool(body.split): raise HTTPException(400,'Select task_ids or a split')
    args=body.model_dump();store=get_store()
    args['task_ids']=list(dict.fromkeys(body.task_ids)) if body.task_ids else [t['task_id'] for t in store.ehr_all('ehr_tasks',split=body.split,status='ready')]
    if not args['task_ids']: raise HTTPException(400,'No ready tasks')
    if any(not (t:=store.ehr_get('ehr_tasks',task_id=i)) or t['status']!='ready' for i in args['task_ids']): raise HTTPException(400,'All tasks must be ready')
    return queue_job(background,'run',args,admin['id'])

@router.get('/runs/{run_group}')
def run_status(run_group: str,admin=Depends(require_admin)):
    events=get_store().list_events(entity_type='ehr_job',entity_id=run_group,limit=10)
    if not events: raise HTTPException(404,'Run not found')
    return {'run_group':run_group,'events':events,'report_path':'/ehr-sandbox/reports/'+run_group}

@router.get('/reports/{run_group}')
def get_report(run_group: str,admin=Depends(require_admin)):
    return report.build(store=get_store(),run_groups=[run_group])

@router.get('/summary')
def summary(admin=Depends(require_admin)):
    from asclepius.ehr_sandbox.rubric import load
    return {**report.build(store=get_store()),'rubric_status':load()['status'],
            'balance':[visit_compiler.balance_split(split,store=get_store(),dry_run=True) for split in ('train','dev','heldout')]}

@router.post('/exclusions')
def exclusion(body: ExclusionBody,admin=Depends(require_admin)):
    store=get_store()
    if not store.ehr_get('ehr_source_exclusions',upload_id=body.upload_id,user_id=body.user_id): store.ehr_insert('ehr_source_exclusions',body.model_dump())
    store.log_event(entity_type='ehr_upload',entity_id=body.upload_id,event_type='ehr_reviewer_excluded',actor=admin['id'],payload=body.model_dump())
    return {'excluded':True}

@router.get('/admin/reviews')
def admin_reviews(status: str | None=None,offset: int=Query(0,ge=0),admin=Depends(require_admin)):
    rows=get_store().ehr_list('ehr_reviews',limit=100,offset=offset,**({'status':status} if status else {}))
    return {'reviews':[public_row(r) for r in rows],'daily_cap':settings().review_daily_cap,'next_offset':offset+100 if len(rows)==100 else None}

@router.get('/reviews/queue')
def queue(user=Depends(require_current_agreement)):
    store=get_store();result=[]
    for a in store.ehr_all('ehr_review_assignments',user_id=user['id']):
        if a['status'] not in reviews.LIVE or a['expires_at']<=audit_now(): continue
        try: reviews.require_live_review_assignment(store,a['review_id'],user['id'])
        except (HTTPException,ValueError): continue
        row=store.ehr_get('ehr_reviews',review_id=a['review_id'])
        result.append({**a,'trigger':row['trigger'],'pay_cents':settings().review_pay_cents})
    return {'assignments':result}

@router.get('/reviews/{review_id}')
def review_view(review_id: str,user=Depends(require_current_agreement)):
    return safe_call(reviews.view,review_id,user['id'],store=get_store())

@router.post('/reviews/{review_id}/verdict')
def review_verdict(review_id: str,body: dict,user=Depends(require_current_agreement)):
    return safe_call(reviews.submit,review_id,user['id'],body,store=get_store())

@router.get('/reviews/{review_id}/outcome')
def outcome(review_id: str,user=Depends(require_current_agreement)):
    return safe_call(reviews.reveal_outcome,review_id,user['id'],store=get_store())

@router.post('/reviews/{review_id}/reflection')
def reflection(review_id: str,body: dict,user=Depends(require_current_agreement)):
    return safe_call(reviews.record_reflection,review_id,user['id'],body,store=get_store())

@router.post('/exports/package')
def export_package(body: ExportBody,admin=Depends(require_admin)):
    args=body.model_dump();buyer=args.pop('buyer_ref');split=args.pop('split')
    result=safe_call(package.build,split,args,buyer,store=get_store())
    return {k:v for k,v in result.items() if not k.endswith('_path')}

@router.get('/exports/{export_id}/{artifact}')
def download_export(export_id: str,artifact: Literal['archive','grader-key'],admin=Depends(require_admin)):
    path=safe_call(package.artifact_path,export_id,artifact,store=get_store())
    return FileResponse(path,filename=Path(path).name,headers={'Cache-Control':'no-store'})
