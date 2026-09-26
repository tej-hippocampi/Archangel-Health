"""Agent rollout driver and control-plane persistence; oracle is never exported."""
from __future__ import annotations
import asyncio
import copy
import json
import re
import uuid
from .common import digest,dumps,identifier,document_text,subject,resource_ref
from .constants import audit_now,settings
from .env import EhrVisitEnv
from .sealed import unseal,seal
from .grader import grade
from .items import key_items
from .terminology import drug,plain_numbers

SYSTEM='You are operating an EHR through tools. Search by MRN, then use the returned Patient.id as patient_id in subsequent tools. Act only on the patient named in the task. Use retrieved evidence; never invent missing lab values or calculator inputs. If an input is unavailable, omit that calculation and document the missing evidence. Follow the exact input names in each tool schema. Place orders with tools; text in your note does not place orders. Confirm successful tool results before documenting actions as completed. Call finish_visit after all actions and the note are complete.'


def effective_key(visit,*,store,connection=None):
    key=unseal(visit['key_enc'])
    for row in store.ehr_all('ehr_key_corrections',visit_id=visit['visit_id'],_connection=connection):
        correction=json.loads(row['correction_json'])
        if isinstance(correction,dict) and 'sealed' in correction: correction=unseal(correction['sealed'])
        if 'replacement_key' in correction:
            key=correction['replacement_key']; continue
        for field in ('assessments','med_changes','orders','referrals'):
            for index,item in enumerate(key.get(field,[])):
                if item['item_id']==correction.get('item_id'):
                    if correction.get('to') is None: key[field].pop(index)
                    else: key[field][index]=correction['to']
                    break
        for field in ('follow_up','escalation'):
            if (key.get(field) or {}).get('item_id')==correction.get('item_id'): key[field]=correction.get('to')
    return key


def scripted(env,key,mode='oracle'):
    """Test/admin sanity only. Every effect uses the public tool action space."""
    def call(tool,**params):
        return env.step({'tool':tool,'input':params})[0].get('observation',{})
    target=env.sandbox.target_patient_id
    call('get_patient',patient_id=target)
    meds=call('search_medications',patient_id=target).get('entry',[])
    labs=call('search_observations',patient_id=target,count=200).get('entry',[])
    if mode=='noop': call('finish_visit'); return env
    docs=call('search_documents',patient_id=target,count=200).get('entry',[])
    for entry in docs: call('read_document',document_id=entry['resource']['id'])
    call('search_conditions',patient_id=target)
    if env.task.get('task_kind','visit').startswith('probe'):
        call('submit_answer',answer=key.get('answer',key)); return env
    plan=[]; assessment=[]; planted=False
    for item in key_items(key):
        if item.get('not_gradable'): continue
        kind=item['type']; params={'patient_id':target}
        if kind=='assessment':
            call('record_assessment',**params,text=item['text'],icd10=item['icd10'])
            assessment.append(item['text']); continue
        if kind=='med':
            matched=next((e['resource'] for e in meds if drug(e['resource']['medicationCodeableConcept']['text'])['name']==item['ingredient']),None)
            textdose=plain_numbers(item.get('to_dose'))
            action=item['action']; dose=re.search(r'(\d+(?:\.\d+)?)\s*(mg|mcg|g|mEq|mL|units?)\b',textdose,re.I)
            frequency=textdose[dose.end():].strip() if dose else 'unspecified'
            if action in ('hold','stop') and matched:
                call('hold_medication' if action=='hold' else 'discontinue_medication',medication_request_id=matched['id'],reason=item.get('reason') or 'reference plan')
            elif dose:
                doseargs={'dose_value':float(dose.group(1)),'dose_unit':dose.group(2),'frequency':frequency}
                if action=='start': call('create_medication_order',**params,drug=item['drug'],route='oral',**doseargs)
                elif matched: call('change_medication_order',medication_request_id=matched['id'],reason=item.get('reason') or 'reference plan',**doseargs)
            plan.append(f"{action.title()} {item['drug']}"+(f" to {textdose}" if textdose else '')+'.')
        elif kind=='lab':
            plan.append(f"Check {item.get('group') or item.get('codes',[''])[0]} in {item.get('timing_days') or 0} days.")
            if mode=='planted' and not planted: planted=True; continue
            call('create_lab_order',**params,code=item.get('group') or item['codes'][0],timing_days=item.get('timing_days') or 0)
        elif kind=='referral':
            call('create_referral',**params,specialty=item['specialty'],reason=item.get('reason') or 'reference plan')
            plan.append('Refer to '+item['specialty']+'.')
        elif kind=='imaging':
            call('create_imaging_order',**params,study=item['text'],reason=item.get('reason') or 'reference plan')
            plan.append('Order '+item['text']+'.')
        elif kind=='follow_up':
            days=item['interval_days']+60 if mode=='planted' and not planted else item['interval_days']; planted=planted or mode=='planted'
            call('schedule_follow_up',**params,interval_days=days)
            plan.append(f'Follow up in {days} days.')
        elif kind=='escalation':
            call('flag_urgent',**params,action=item['action'],reason=item.get('reason') or 'urgent clinical concern')
            plan.append(item['action']+'.')
    evidence=[]
    for entry in labs:
        r=entry['resource']; q=r.get('valueQuantity',{})
        if 'value' in q: evidence.append(f"{r['code'].get('text','Lab')} {q['value']} on {r.get('effectiveDateTime','')[:10]};")
    call('write_visit_note',patient_id=target,assessment=' '.join(assessment)+'\n'+'\n'.join(evidence)[-3500:],plan=' '.join(plan)+' '+'; '.join(key.get('counseling',[])))
    call('finish_visit')
    return env


async def drive(env,*,model,harness='native_tools'):
    from ai.llm_client import call_llm,first_text
    from asclepius.environments.rollout import _extract_json
    opening,_=env.reset(); messages=[{'role':'user','content':dumps(opening)}]; provider=None
    for turn in range(env.budget*3):
        if env.terminated or env.truncated: break
        kwargs={'model':model,'max_tokens':2000,'temperature':0}
        system=SYSTEM
        if harness=='native_tools': kwargs['tools']=env.action_space()
        else: system+=' Respond with exactly one JSON object {"tool":"name","input":{...}} per turn. No prose, duplicate objects, arrays or additional calls. Wait for the tool result before choosing the next action. Tools: '+dumps(env.action_space())
        response,audit=await call_llm(role='ehr_agent',purpose='ehr_agent_rollout',prompt_id='ehr_agent_v1',system=system,messages=messages,json_object=harness=='json_protocol',**kwargs)
        provider=audit.get('provider'); blocks=[]
        for block in getattr(response,'content',[]):
            if getattr(block,'type',None)=='tool_use': blocks.append({'type':'tool_use','id':block.id,'name':block.name,'input':block.input})
            elif getattr(block,'type',None)=='openai_response_item': blocks.append({'type':'openai_response_item','item':block.item})
            elif getattr(block,'type',None)=='text' and getattr(block,'text',''): blocks.append({'type':'text','text':block.text})
        if harness=='native_tools' and any(b['type']=='tool_use' for b in blocks):
            messages.append({'role':'assistant','content':blocks})
            results=[]
            for b in blocks:
                if b['type']!='tool_use': continue
                obs=env.step({'tool':b['name'],'input':b['input']})[0]
                results.append({'type':'tool_result','tool_use_id':b['id'],'content':dumps(obs)})
            messages.append({'role':'user','content':results})
        else:
            text=first_text(response); action=_extract_json(text)
            if action and 'tool' in action:
                obs=env.step(action)[0]
            else:
                env.step({'type':'thought','content':text}); obs={'error':'No tool was executed. Respond with exactly one JSON object containing tool and input; wait for its result.'}
            messages.extend([{'role':'assistant','content':text or ' '},{'role':'user','content':dumps(obs)}])
    if not env.terminated and not env.truncated: env.truncated=True; env.terminated_by='error'
    return provider


async def run(task_id,*,model,k=1,harness='native_tools',store=None,run_group=None):
    from asclepius.store import get_store
    store=store or get_store()
    if not 1<=k<=20: raise ValueError('k must be 1 through 20')
    if harness not in ('native_tools','json_protocol'): raise ValueError('unsupported harness')
    task=store.ehr_get('ehr_tasks',task_id=task_id)
    if not task or task['status']!='ready': raise ValueError('task is not ready')
    visit=store.ehr_get('ehr_visits',visit_id=task['visit_id'])
    chart=store.ehr_get('ehr_charts',chart_id=visit['chart_id'])
    if chart['status']!='built' or visit['key_audit'] not in ('passed','waived','not_sampled'): raise ValueError('chart or key audit is not ready')
    key=unseal(task['probe_key_enc']) if task.get('probe_key_enc') else effective_key(visit,store=store)
    run_group=run_group or 'ehrg-'+uuid.uuid4().hex[:16]; results=[]
    # Each episode has its own overlay. Bounded concurrency applies across repeats.
    semaphore=asyncio.Semaphore(settings().harness_concurrency)
    async def one(index):
        async with semaphore:
            rollout_id=identifier('ehrr',[run_group,task_id,index]); env=EhrVisitEnv(task); env.reset()
            provider='scripted'
            if model.startswith('scripted:'):
                mode=model.split(':',1)[1]
                if mode not in ('noop','oracle','planted'): raise ValueError('unsupported scripted agent')
                scripted(env,key,mode)
            else:
                try: provider=await drive(env,model=model,harness=harness)
                except Exception as exc:
                    env.truncated=True; env.terminated_by='error'
                    env.trajectory.append({'step':len(env.trajectory)+1,'type':'error','content':type(exc).__name__})
                    provider='error'
            rollout=env.rollout()
            from . import rubric as rubric_module
            notes='\n'.join(document_text(r) for r in rollout['overlay'] if r['resourceType']=='DocumentReference')
            rubric_result=None
            if notes:
                context=[r for r in env.sandbox.resources() if subject(r)==env.sandbox.target_patient_id]
                try: rubric_result=await rubric_module.judge(notes,context,store=store)
                except Exception: rubric_result={'score':None,'mode':'deterministic_only','reason':'rubric_judge_failed'}
            result=grade(task,rollout,key,rubric=rubric_result); now=audit_now()
            # Store enough replay evidence to recompute after physician corrections.
            trace={'trajectory':rollout['trajectory'],'write_log':rollout['write_log'],'wrong_patient':rollout['wrong_patient'],
                   'answer':rollout['answer'],'calculations':rollout['calculations'],'tool_calls':rollout['tool_calls'],
                   'verification':public_verification(result),'verification_enc':seal(result),'rubric_enc':seal(rubric_result) if rubric_result else None}
            with store._conn() as conn:
                store._immediate(conn)
                store.ehr_insert('ehr_rollouts',{'rollout_id':rollout_id,'task_id':task_id,'model':model,'provider':provider,
                    'harness':model if provider=='scripted' else harness,'run_group':run_group,'trajectory_json':dumps(trace),
                    'access_log_json':dumps(rollout['access_log']),'writes_json':dumps(rollout['overlay']),
                    'rejected_writes_json':dumps(rollout['rejected_writes']),'terminated_by':rollout['terminated_by'],
                    'provisional_reward':result['reward'],'hard_fail':int(result['hard_fail']),'hard_fail_reason':result.get('hard_fail_reason'),
                    'created_at':now,'updated_at':now},_connection=conn)
                for cp in result['checkpoints']:
                    store.ehr_insert('ehr_checkpoints',{'checkpoint_id':identifier('ehrcp',[rollout_id,cp['kind']]),'rollout_id':rollout_id,
                        'kind':cp['kind'],'score':cp['score'],'verdict':cp['verdict'],'items_json':dumps(public_verification(cp['items'])),'grader':cp['grader']},_connection=conn)
            # Review routing is control-plane-only and is added by M5.
            from . import reviews
            # A provider failure (bad model id, outage, timeout) says nothing about
            # the plan: keep the graded evidence, never offer it for paid review.
            if provider=='error': store.ehr_update('ehr_rollouts',{'rollout_id':rollout_id},{'status':'provider_error','final_reward':None})
            elif hasattr(reviews,'route_rollout'): reviews.route_rollout(rollout_id,store=store)
            else: store.ehr_update('ehr_rollouts',{'rollout_id':rollout_id},{'final_reward':result['reward'],'status':'final'})
            return {'rollout_id':rollout_id,**public_verification(result)}
    results=await asyncio.gather(*(one(i) for i in range(k)))
    return {'run_group':run_group,'rollouts':results}


def sanity(task,key):
    results={}
    for name in ('noop','oracle'):
        env=EhrVisitEnv(task); env.reset(); scripted(env,key,name)
        results[name]=grade(task,env.rollout(),key)
    results['passed']=results['noop']['reward']<=.15 and (results['oracle']['hard_fail'] or results['oracle']['reward']>=.9)
    results['doctor_flagged']=results['oracle']['hard_fail']
    return results


def public_verification(value):
    """Expected decisions remain encrypted; public diagnostics use opaque item IDs."""
    if isinstance(value,dict):
        result={}
        for key,child in value.items():
            if key in ('source_span','extraction','key_enc','outcome_enc','probe_key_enc'): continue
            result[key]={'item_id':child.get('item_id')} if key=='key' and isinstance(child,dict) else public_verification(child)
        return result
    if isinstance(value,list): return [public_verification(v) for v in value]
    return value


def private_verification(trace):
    return unseal(trace['verification_enc']) if trace.get('verification_enc') else trace['verification']
