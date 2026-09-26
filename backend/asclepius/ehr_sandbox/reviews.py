"""Strict assignment-gated physician review, independent rounds and atomic earnings."""
from __future__ import annotations
import copy
import json
import logging
from collections import Counter
from datetime import datetime,timedelta
from fastapi import HTTPException
from asclepius.store import get_store
from asclepius import allocation,capabilities,tiering,review,physician_agreement
from .common import digest,dumps,identifier,subject,document_text
from .constants import audit_now,settings
from .items import key_items
from .terminology import normalize,drug
from .sealed import unseal,seal
from .review_policy import render_plan_item,signature,_disputes

logger=logging.getLogger(__name__)

LIVE=('offered','claimed')
CLOSED=('resolved','unresolved','cancelled')
RATINGS={'appropriate','acceptable_alternative','inappropriate','harmful'}


def load_candidates(store):
    """Same eligibility construction as routers/asclepius_admin._allocation_inputs."""
    result=[]
    for user in store.list_users():
        if user.get('role')!='evaluator' or not user.get('active',1) or user.get('is_mock') or user.get('verification_status','approved')!='approved': continue
        match,_=tiering.domain_match(user,'nephrology')
        candidate=allocation.Physician(user_id=user['id'],can_label=capabilities.can(user,capabilities.LABEL),can_review=review.can_review(user),
            domain_match=match,real_data_approved=bool(user.get('real_data_approved')))
        if not allocation._eligible_to_review(candidate,allocation.Case('ehr-review','nephrology',True))[0]: continue
        if physician_agreement.gate_enabled() and physician_agreement.resignature_reason(store.latest_physician_agreement(user['id'])) is not None: continue
        result.append({'user':user,'domain_match':match})
    return result


def _chart(store,visit_id,conn=None):
    visit=store.ehr_get('ehr_visits',visit_id=visit_id,_connection=conn)
    if not visit: raise ValueError('visit not found')
    chart=store.ehr_get('ehr_charts',chart_id=visit['chart_id'],_connection=conn)
    if not chart or chart['status']!='built': raise ValueError('chart is not eligible for review')
    return visit,chart


def require_live_review_assignment(store,review_id,user_id,*,connection=None,submitted=False):
    row=store.ehr_get('ehr_review_assignments',review_id=review_id,user_id=user_id,_connection=connection)
    allowed=('submitted',) if submitted else LIVE
    if not row or row['status'] not in allowed or (not submitted and row['expires_at']<=audit_now()):
        raise HTTPException(403,'A current EHR review assignment is required.')
    # Recheck clearance/exclusions on every access and submit, including admins.
    if user_id not in {c['user']['id'] for c in load_candidates(store)}: raise HTTPException(403,'Reviewer eligibility is no longer current.')
    review_row=store.ehr_get('ehr_reviews',review_id=review_id,_connection=connection)
    _,chart=_chart(store,review_row['visit_id'],connection)
    if store.ehr_get('ehr_source_exclusions',upload_id=chart['upload_id'],user_id=user_id,_connection=connection):
        raise HTTPException(403,'Source-practice exclusions prohibit this review.')
    return row


def create_review(visit_id,trigger,items,*,rollout_id=None,scope='rollout',store=None):
    store=store or get_store(); _chart(store,visit_id)
    dedupe=f'{visit_id if scope=="visit" else rollout_id}|{trigger}'
    review_id=identifier('ehrrev',dedupe); now=audit_now()
    items=copy.deepcopy(items)
    for index,item in enumerate(items):
        item.setdefault('item_id',f'item-{index}'); item['signature']=signature(item)
    with store._conn() as conn:
        store._immediate(conn)
        row=store.ehr_get('ehr_reviews',dedupe_key=dedupe,_connection=conn)
        if not row:
            store.ehr_insert('ehr_reviews',{'review_id':review_id,'scope':scope,'rollout_id':rollout_id if scope=='rollout' else None,
                'visit_id':visit_id,'trigger':trigger,'dedupe_key':dedupe,'items_json':dumps({'sealed':seal(items)}),
                'blind_order':'a_is_doctor' if int(digest(review_id)[:8],16)%2 else 'a_is_agent','created_at':now},_connection=conn)
        if rollout_id and not store.ehr_get('ehr_review_rollouts',review_id=review_id,rollout_id=rollout_id,_connection=conn):
            store.ehr_insert('ehr_review_rollouts',{'review_id':review_id,'rollout_id':rollout_id,'item_ids_json':dumps([i['item_id'] for i in items])},_connection=conn)
    select_reviewers(review_id,1,store=store)
    return store.ehr_get('ehr_reviews',review_id=review_id)


def select_reviewers(review_id,n=1,*,store=None):
    store=store or get_store(); candidates=load_candidates(store); now=audit_now(); offered=[]
    with store._conn() as conn:
        store._immediate(conn)
        row=store.ehr_get('ehr_reviews',review_id=review_id,_connection=conn)
        if not row or row['status'] in CLOSED: return []
        _,chart=_chart(store,row['visit_id'],conn)
        assignments=store.ehr_all('ehr_review_assignments',limit=10000,_connection=conn)
        round_no={'open':1,'needs_second':2,'needs_tiebreak':3}[row['status']]
        if any(a['review_id']==review_id and a['round']==round_no and a['status'] in LIVE+('submitted',) for a in assignments): return []
        # Count reviews first assigned today; backlog creation itself consumes no budget.
        first_offered={}
        for a in assignments: first_offered[a['review_id']]=min(first_offered.get(a['review_id'],a['offered_at']),a['offered_at'])
        assigned_today={rid for rid,at in first_offered.items() if at[:10]==now[:10]}
        if review_id not in first_offered and len(assigned_today)>=settings().review_daily_cap:
            store.ehr_update('ehr_reviews',{'review_id':review_id},{'reason':'daily_cap'},_connection=conn); return []
        excluded={a['user_id'] for a in assignments if a['review_id']==review_id}
        excluded.update(a['user_id'] for a in store.ehr_all('ehr_source_exclusions',upload_id=chart['upload_id'],_connection=conn))
        ranked=[]
        for candidate in candidates:
            uid=candidate['user']['id']; own=[a for a in assignments if a['user_id']==uid]
            load=sum(a['status'] in LIVE and a['expires_at']>now for a in own)
            if uid in excluded or load>=settings().review_max_open: continue
            ranked.append((load,-candidate['domain_match'],max((a['offered_at'] for a in own),default=''),digest([review_id,uid]),candidate))
        for *_,candidate in sorted(ranked)[:min(n,1)]:
            uid=candidate['user']['id']; aid=identifier('ehra',[review_id,uid]); instant=datetime.fromisoformat(now)
            assignment={'review_assignment_id':aid,'review_id':review_id,'user_id':uid,'round':round_no,'offered_at':now,
                        'due_at':(instant+timedelta(hours=settings().review_due_hours)).isoformat(),
                        'expires_at':(instant+timedelta(hours=96)).isoformat()}
            store.ehr_insert('ehr_review_assignments',assignment,_connection=conn); offered.append((assignment,candidate['user']))
        store.ehr_update('ehr_reviews',{'review_id':review_id},{'reason':None if offered else 'no_eligible_reviewer'},_connection=conn)
    from notifications import notify_person, _portal_base
    from urllib.parse import urlsplit, urlunsplit
    import realm
    import html
    origin=urlsplit(_portal_base())
    path='/sandbox/asclepius' if realm.is_sandbox() else '/asclepius'
    review_url=urlunsplit((origin.scheme,origin.netloc,path,'',f'ehr-review/{review_id}'))
    for assignment,user in offered:
        notify_person(store,kind='ehr_review_offer',to=user.get('email',''),subject='A nephrology review is waiting',
                      body_html=f'<p>A 5–10 minute nephrology review is waiting, ${settings().review_pay_cents/100:.2f}.</p><p><a href="{html.escape(review_url,quote=True)}">Open review</a></p>',
                      dedupe_key=assignment['review_assignment_id'])
    return [a for a,_ in offered]


def _opened_at(store,assignment_id):
    """When the reviewer first opened the review, or None if never opened."""
    opened=[e['occurred_at'] for e in store.list_events(entity_type='ehr_review_assignment',entity_id=assignment_id)
            if e['event_type']=='ehr_review_opened']
    return min(opened) if opened else None


def view(review_id,user_id,*,store=None):
    store=store or get_store(); assignment=require_live_review_assignment(store,review_id,user_id)
    # Pay is gated on the server's clock from this first open, not the browser's timer.
    if _opened_at(store,assignment['review_assignment_id']) is None:
        store.log_event(entity_type='ehr_review_assignment',entity_id=assignment['review_assignment_id'],
                        event_type='ehr_review_opened',actor=user_id)
    row=store.ehr_get('ehr_reviews',review_id=review_id); visit,chart=_chart(store,row['visit_id'])
    tasks=store.ehr_all('ehr_tasks',visit_id=visit['visit_id'],task_kind='visit')
    if tasks:
        bundle=json.loads(tasks[0]['snapshot_json']); target=bundle['identifier']['value']
        resources=[e['resource'] for e in bundle['entry'] if subject(e['resource'])==target]
    else:
        from .visit_compiler import slice_resources,synthetic_resources,assert_fhir_slice
        visible=slice_resources(json.loads(chart['resources_json']),visit['offset_days']);assert_fhir_slice(visible,visit['offset_days'])
        resources,_,_=synthetic_resources(visible,visit['offset_days'],int(digest(visit['visit_id'])[:8],16),0)
        target=next(r['id'] for r in resources if r['resourceType']=='Patient')
    items=[]
    for index,item in enumerate(review_items(row)):
        plans=[render_plan_item(item.get('key')),render_plan_item(item.get('actual'))]
        if row['blind_order']=='a_is_agent': plans.reverse()
        items.append({'item_id':item['item_id'],'plan_a':plans[0],'plan_b':plans[1],**({'finding':item['rule']} if item.get('rule') else {}),**({'criterion':{k:v for k,v in item['criterion'].items() if k in ('id','description','points','critical')}} if item.get('criterion') else {})})
        if row['trigger'] in ('outcome_flag','dose_sample'): items[-1]['proposed_plan']=render_plan_item(item.get('key'))
    result={'review_id':review_id,'trigger':row['trigger'],'round':assignment['round'],'due_at':assignment['due_at'],
            'chart':resources,'items':items,'pay_cents':settings().review_pay_cents,'outcome_available_after_submit':row['trigger']=='outcome_flag'}
    from .harness import effective_key
    from .items import medication_amount,actual_items
    def medication_plan(changes):
        current={}
        for medication in resources:
            if medication['resourceType']!='MedicationRequest' or medication.get('status')!='active': continue
            name=drug(medication['medicationCodeableConcept']['text'])['name'];amount=medication_amount(medication)
            current[name]={'type':'med','ingredient':name,'action':'continue',
                'dose':amount['value'] if amount else None,'dose_unit':amount['unit'] if amount else None}
        for change in changes:
            if change['type']!='med': continue
            if change['action'] in ('stop','hold'): current.pop(change['ingredient'],None)
            else: current[change['ingredient']]=change
        return [render_plan_item(current[name]) for name in sorted(current)]
    proposed=medication_plan(key_items(effective_key(visit,store=store)))
    if row['trigger']=='outcome_flag':
        result['proposed_medications']=proposed
    elif row.get('rollout_id') and row['trigger']=='disagreement':
        rollout=store.ehr_get('ehr_rollouts',rollout_id=row['rollout_id'])
        if rollout:
            plans=[proposed,medication_plan(actual_items(resources,json.loads(rollout['writes_json']),target))]
            if row['blind_order']=='a_is_agent': plans.reverse()
            result.update(plan_a_medications=plans[0],plan_b_medications=plans[1])
    if row['trigger']=='key_audit':
        extraction=json.loads(chart['extraction_json'])
        note_ids=next((v['document_ids'] for v in extraction['visits'] if v['encounter_ref']==visit['encounter_ref']),[])
        result['worksheet']='\n'.join(document_text(r) for r in json.loads(chart['resources_json']) if r['resourceType']=='DocumentReference' and r['id'] in note_ids)
        result['extracted_key']=unseal(visit['key_enc'])
    if row['trigger']=='rubric_sample':
        rollout=store.ehr_get('ehr_rollouts',rollout_id=row['rollout_id'])
        result['agent_note']='\n'.join(document_text(r) for r in json.loads(rollout['writes_json']) if r['resourceType']=='DocumentReference')
        rubric=unseal(json.loads(rollout['trajectory_json'])['rubric_enc']) if json.loads(rollout['trajectory_json']).get('rubric_enc') else {}
        result['rubric']={'rubric_version':rubric.get('rubric_version')}
    return result


def mapped_verdict(a,b,blind):
    doctor,agent=(a,b) if blind=='a_is_doctor' else (b,a)
    if agent=='harmful': return 'harmful'
    if doctor in ('inappropriate','harmful'):
        return 'doctor_wrong' if agent in ('appropriate','acceptable_alternative') else 'both_wrong'
    return 'valid_alternative' if agent in ('appropriate','acceptable_alternative') else 'agent_wrong'


def submit(review_id,user_id,body,*,store=None):
    store=store or get_store()
    if body.get('confidence') not in ('high','low'): raise ValueError('confidence is required')
    if not isinstance(body.get('seconds_spent'),int) or body['seconds_spent']<0: raise ValueError('seconds_spent must be a nonnegative integer')
    now=audit_now(); next_round=False
    from asclepius import compensation
    payable=compensation.accrues_payment(store.get_user_by_id(user_id))
    opened_at=_opened_at(store,identifier('ehra',[review_id,user_id]))
    with store._conn() as conn:
        store._immediate(conn)
        previous=store.ehr_get('ehr_review_verdicts',review_id=review_id,reviewer_user_id=user_id,_connection=conn)
        if previous:
            # A retry may read its own receipt; it cannot alter the verdict or earning.
            existing=verdict_data(previous)
            if existing['submission']!=body: raise ValueError('verdict already submitted with different content')
            return {'verdict_id':previous['verdict_id'],'already_submitted':True}
        assignment=require_live_review_assignment(store,review_id,user_id,connection=conn)
        row=store.ehr_get('ehr_reviews',review_id=review_id,_connection=conn)
        expected=review_items(row); answers=body.get('items',[])
        if not isinstance(answers,list) or any(not isinstance(i,dict) for i in answers): raise ValueError('items must be a list of answers')
        mapped={}; normalized_key=None
        if row['trigger']=='key_audit':
            audit=copy.deepcopy(body.get('key_audit') or {})
            if len(str(audit.get('rationale','')).strip())<20: raise ValueError('Key audit needs a substantive rationale')
            mapped=_key_audit_vote(store,row,audit,connection=conn)
            normalized_key=audit.get('replacement_key')
        else:
            if len(answers)!=len(expected) or {i.get('item_id') for i in answers}!={i['item_id'] for i in expected}: raise ValueError('answer every item exactly once')
            for item in answers:
                if len(str(item.get('rationale','')).strip())<20: raise ValueError('Each item needs a rationale of at least 20 characters')
                if row['trigger']=='safety':
                    if item.get('safety_decision') not in ('confirmed','false_positive','uncertain'): raise ValueError('Each safety finding needs a decision')
                    mapped[item['item_id']]={'confirmed':'safety_confirmed','false_positive':'safety_false_positive','uncertain':'unresolved'}[item['safety_decision']]
                elif row['trigger']=='rubric_sample':
                    if not isinstance(item.get('met'),bool): raise ValueError('Every rubric criterion needs a true/false judgement')
                    mapped[item['item_id']]='rubric_met' if item['met'] else 'rubric_unmet'
                elif row['trigger'] in ('outcome_flag','dose_sample'):
                    if item.get('reference_decision') not in RATINGS|{'uncertain'}:
                        raise ValueError('Assess the proposed plan using only the pre-visit chart')
                    mapped[item['item_id']]='unresolved' if item['reference_decision']=='uncertain' else 'doctor_wrong' if item['reference_decision'] in ('inappropriate','harmful') else 'reference_supported'
                else:
                    if item.get('plan_a') not in RATINGS or item.get('plan_b') not in RATINGS: raise ValueError('both plan ratings are required')
                    if item.get('better') not in ('A','B','equivalent'): raise ValueError('overall preference is required')
                    mapped[item['item_id']]=mapped_verdict(item['plan_a'],item['plan_b'],row['blind_order'])
                    if mapped[item['item_id']]=='harmful' and item.get('critical') is True: mapped[item['item_id']]='harmful_critical'
        verdict_id=identifier('ehrv',assignment['review_assignment_id'])
        stored={'submission':body,'mapped':mapped,'normalized_key':normalized_key}
        store.ehr_insert('ehr_review_verdicts',{'verdict_id':verdict_id,'review_id':review_id,'review_assignment_id':assignment['review_assignment_id'],
            'reviewer_user_id':user_id,'round':assignment['round'],'verdict_json':dumps({'sealed':seal(stored)}),'confidence':body['confidence'],
            'seconds_spent':body['seconds_spent'],'submitted_at':now},_connection=conn)
        store.ehr_update('ehr_review_assignments',{'review_assignment_id':assignment['review_assignment_id']},{'status':'submitted'},_connection=conn)
        from asclepius.payments import KIND_EHR_REVIEW
        aid=assignment['review_assignment_id']
        straight=len(answers)>=5 and len({(i.get('plan_a'),i.get('plan_b'),i.get('better'),i.get('safety_decision'),i.get('met'),i.get('reference_decision'),i.get('critical')) for i in answers})==1
        # The browser's timer is advisory; the server's clock since the first open
        # bounds it. A review submitted without ever being opened is held.
        elapsed=(datetime.fromisoformat(now)-datetime.fromisoformat(opened_at)).total_seconds() if opened_at else 0
        fast=min(body['seconds_spent'],elapsed)<60
        if payable:  # equity-only advisors record the verdict but accrue no cash
            earning_id=identifier('earn',aid)
            store.insert_earning(earning_id=earning_id,user_id=user_id,kind=KIND_EHR_REVIEW,ref_id=aid,
                amount_cents=settings().review_pay_cents,rate_cents=settings().review_pay_cents,status='accrued',accrued_at=now,note=f'ehr_review:{review_id}',_connection=conn)
            if not fast and not straight:
                store.resolve_earning(kind=KIND_EHR_REVIEW,ref_id=aid,status='approved',resolved_at=now,only_from=['accrued'],_connection=conn)
            else:
                # Held for a person: the fourteen-day auto-approve sweep skips held rows.
                store.set_earning_quality(earning_id,multiplier=1.0,reasons=[r for r,hit in (('fast_review',fast),('straight_lined',straight)) if hit],
                                          version='ehr_review_v1',hold=True,_connection=conn)
        verdicts=store.ehr_all('ehr_review_verdicts',review_id=review_id,_connection=conn)
        mapped_rows=[verdict_data(v)['mapped'] for v in verdicts]
        resolved=None
        if len(verdicts)==1 and body['confidence']=='high' and row['trigger']!='safety' and not any(v in ('doctor_wrong','harmful','harmful_critical','both_wrong') or v.startswith('key_corrected:') for v in mapped.values()): resolved=mapped
        elif len(verdicts)>=2 and mapped_rows[0]==mapped_rows[1]: resolved=mapped_rows[0]
        elif len(verdicts)>=3:
            resolved={item_id:(Counter(v[item_id] for v in mapped_rows).most_common(1)[0][0]
                      if Counter(v[item_id] for v in mapped_rows).most_common(1)[0][1]>=2 else 'unresolved') for item_id in mapped}
        if resolved is None:
            store.ehr_update('ehr_reviews',{'review_id':review_id},{'status':'needs_second' if len(verdicts)==1 else 'needs_tiebreak'},_connection=conn)
            next_round=True
        else:
            _resolve(store,conn,row,expected,resolved,verdicts,now)
    if next_round: select_reviewers(review_id,1,store=store)
    return {'verdict_id':verdict_id,'already_submitted':False,'status':store.ehr_get('ehr_reviews',review_id=review_id)['status']}


def _resolve(store,conn,row,items,resolved,verdicts,now):
    """Resolution, append-only correction/cache and dependent rewards commit together."""
    unresolved=all(v=='unresolved' for v in resolved.values())
    resolution={'items':resolved,'agreement_pairs':[verdict_data(v)['mapped'] for v in verdicts[:2]]}
    store.ehr_update('ehr_reviews',{'review_id':row['review_id']},{'status':'unresolved' if unresolved else 'resolved',
        'resolution_json':dumps(resolution),'resolved_at':now,'reason':None},_connection=conn)
    if row['trigger']=='key_audit' and 'key-audit' in resolved:
        decision=resolved['key-audit']
        if decision.startswith('key_corrected:'):
            source=next(verdict_data(v) for v in verdicts if verdict_data(v)['mapped'].get('key-audit')==decision)
            replacement=source['normalized_key']
            store.ehr_insert('ehr_key_corrections',{'correction_id':identifier('ehrkc',row['review_id']),'visit_id':row['visit_id'],'review_id':row['review_id'],
                'correction_json':dumps({'sealed':seal({'replacement_key':replacement})}),'created_at':now},_connection=conn)
        store.ehr_update('ehr_visits',{'visit_id':row['visit_id']},{'key_audit':'failed' if decision=='unresolved' else 'passed'},_connection=conn)
        return
    for item in items:
        verdict=resolved[item['item_id']]; signature_value=item['signature']; cache_id=digest([row['visit_id'],signature_value])
        if not store.ehr_get('ehr_verdict_cache',cache_key=cache_id,_connection=conn):
            store.ehr_insert('ehr_verdict_cache',{'cache_key':cache_id,'visit_id':row['visit_id'],'item_signature':signature_value,
                'verdict':verdict,'source_review_id':row['review_id'],'created_at':now},_connection=conn)
        if row['trigger'] not in ('safety','key_audit','rubric_sample','dose_sample') and verdict in ('doctor_wrong','both_wrong') and (item.get('key') or {}).get('item_id'):
            corrected=None
            if verdict=='doctor_wrong' and item.get('actual'):
                corrected=copy.deepcopy(item['actual']);corrected.pop('resource',None)
                corrected['item_id']=item['key']['item_id']
                if corrected['type']=='med':
                    corrected['to_dose']=f"{corrected['dose']} {corrected.get('dose_unit') or 'mg'} daily" if corrected.get('dose') is not None else None
                if corrected['type'] in ('lab','imaging'): corrected['kind']=corrected['type']
                if corrected['type']=='lab': corrected['loinc_group']=corrected.get('group')
            store.ehr_insert('ehr_key_corrections',{'correction_id':identifier('ehrkc',[row['review_id'],item['item_id']]),'visit_id':row['visit_id'],
                'review_id':row['review_id'],'correction_json':dumps({'sealed':seal({'item_id':item['key']['item_id'],'from':item['key'],'to':corrected,'reason':verdict})}),
                'created_at':now},_connection=conn)
    if row['trigger']=='dose_sample' and any(v in ('doctor_wrong','unresolved') for v in resolved.values()):
        rollout=store.ehr_get('ehr_rollouts',rollout_id=row['rollout_id'],_connection=conn)
        store.ehr_update('ehr_tasks',{'task_id':rollout['task_id']},{'status':'excluded'},_connection=conn)
    if row['trigger']=='key_audit':
        failed=any(v in ('doctor_wrong','both_wrong','harmful','agent_wrong','unresolved') for v in resolved.values())
        store.ehr_update('ehr_visits',{'visit_id':row['visit_id']},{'key_audit':'failed' if failed else 'passed'},_connection=conn)
    _resolve_cached_pending(store,conn,row['visit_id'],now)
    # Corrections and cached alternatives affect every rollout of this visit.
    for task in store.ehr_all('ehr_tasks',visit_id=row['visit_id'],_connection=conn):
        for rollout in store.ehr_all('ehr_rollouts',task_id=task['task_id'],_connection=conn):
            recompute_rollout(rollout['rollout_id'],store=store,connection=conn)


def route_rollout(rollout_id,*,store=None):
    store=store or get_store(); rollout=store.ehr_get('ehr_rollouts',rollout_id=rollout_id)
    task=store.ehr_get('ehr_tasks',task_id=rollout['task_id']); visit,chart=_chart(store,task['visit_id'])
    from .harness import private_verification
    verification=private_verification(json.loads(rollout['trajectory_json']))
    if task['task_kind'].startswith('probe'):
        if task['task_kind']=='probe_dose' and int(digest(rollout_id)[:8],16)%100<10:
            reference=unseal(task['probe_key_enc']);answer=reference['answer']
            item={'type':'med','item_id':'dose-reference','drug':reference.get('drug','renal dose reference'),
                  'action':answer['action'],'dose':answer['daily_dose_mg'],'dose_unit':'mg'}
            create_review(visit['visit_id'],'dose_sample',[{'item_id':'dose-reference','key':item,'actual':None}],rollout_id=rollout_id,store=store)
        with store._conn() as conn:
            store._immediate(conn);recompute_rollout(rollout_id,store=store,connection=conn)
        return
    disputed=_disputes(verification)
    unknown=[i for i in disputed if not store.ehr_get('ehr_verdict_cache',cache_key=digest([visit['visit_id'],i['signature']]))]
    if unknown: create_review(visit['visit_id'],'disagreement',unknown,rollout_id=rollout_id,store=store)
    if verification['hard_fail']:
        create_review(visit['visit_id'],'safety',[{'item_id':identifier('finding',[s['rule_id'],s.get('item_id'),i]),'key':None,'actual':{'type':'safety','text':s['reason']},'rule':s} for i,s in enumerate(verification['safety'])],rollout_id=rollout_id,store=store)
    if visit['has_next_visit']:
        from .outcome_rules import evaluate
        from .visit_compiler import slice_resources
        from .harness import effective_key
        flags=evaluate(slice_resources(json.loads(chart['resources_json']),visit['offset_days']),unseal(visit['outcome_enc']),effective_key(visit,store=store))
        if flags:
            key=effective_key(visit,store=store)
            items=[{'item_id':i['item_id'],'key':i,'actual':None,'outcome_signals':flags} for i in key_items(key) if i['type']!='assessment']
            create_review(visit['visit_id'],'outcome_flag',items,rollout_id=rollout_id,scope='visit',store=store)
    trace=json.loads(rollout['trajectory_json']);rubric=unseal(trace['rubric_enc']) if trace.get('rubric_enc') else {}
    if rubric and rubric.get('mode')=='rubric_llm' and int(digest(rollout_id)[:8],16)/0xffffffff<settings().rubric_sample_rate:
        create_review(visit['visit_id'],'rubric_sample',[{'item_id':c['id'],'criterion':c,'key':None,'actual':None} for c in rubric['criteria']],rollout_id=rollout_id,store=store)
    with store._conn() as conn:
        store._immediate(conn);recompute_rollout(rollout_id,store=store,connection=conn)


def recompute_rollout(rollout_id,*,store,connection):
    row=store.ehr_get('ehr_rollouts',rollout_id=rollout_id,_connection=connection)
    if row['status']=='provider_error' or row['provider']=='error': return  # an outage is never a model score
    linked=store.ehr_all('ehr_review_rollouts',rollout_id=rollout_id,_connection=connection)
    reviews=[store.ehr_get('ehr_reviews',review_id=r['review_id'],_connection=connection) for r in linked]
    if any(r['status'] not in CLOSED for r in reviews):
        store.ehr_update('ehr_rollouts',{'rollout_id':rollout_id},{'status':'awaiting_review','final_reward':None},_connection=connection);return
    from .harness import effective_key
    from .grader import grade
    task=store.ehr_get('ehr_tasks',task_id=row['task_id'],_connection=connection)
    if task['task_kind']=='probe_dose' and task['status']=='excluded':
        store.ehr_update('ehr_rollouts',{'rollout_id':rollout_id},{'status':'reference_excluded','final_reward':None},_connection=connection);return
    visit=store.ehr_get('ehr_visits',visit_id=task['visit_id'],_connection=connection)
    trace=json.loads(row['trajectory_json']); replay={**trace,'overlay':json.loads(row['writes_json']),'access_log':json.loads(row['access_log_json']),
        'rejected_writes':json.loads(row['rejected_writes_json'] or '[]'),'terminated_by':row['terminated_by']}
    key=unseal(task['probe_key_enc']) if task.get('probe_key_enc') else effective_key(visit,store=store,connection=connection)
    decisions={}
    from .harness import private_verification,public_verification
    for item in _disputes(private_verification(trace)):
        cached=store.ehr_get('ehr_verdict_cache',cache_key=digest([visit['visit_id'],item['signature']]),_connection=connection)
        if cached: decisions[item['item_id']]={'verdict':cached['verdict'],'key_id':(item.get('key') or {}).get('item_id'),
                                              'actual_id':(item.get('actual') or {}).get('item_id')}
    for review_row in reviews:
        if review_row['trigger']=='outcome_flag' and review_row['status'] in ('resolved','unresolved'):
            resolved=json.loads(review_row['resolution_json'] or '{}').get('items',{})
            for item in review_items(review_row):
                if resolved.get(item['item_id'])=='unresolved': decisions['outcome-'+item['item_id']]={'verdict':'unresolved','key_id':item['key']['item_id']}
        if review_row['trigger']!='safety' or review_row['status']!='resolved': continue
        resolved=json.loads(review_row['resolution_json'])['items']
        for item in review_items(review_row):
            finding=item.get('rule',{})
            decisions[item['item_id']]={'verdict':resolved.get(item['item_id'],'unresolved'),'rule_id':finding.get('rule_id'),'finding_item_id':finding.get('item_id'),'fingerprint':finding.get('fingerprint')}
    rubric=unseal(trace['rubric_enc']) if trace.get('rubric_enc') else None
    for review_row in reviews:
        if review_row['trigger']!='rubric_sample' or review_row['status']!='resolved' or not rubric: continue
        resolved=json.loads(review_row['resolution_json'])['items']
        for criterion in rubric['criteria']:
            if resolved.get(criterion['id']) in ('rubric_met','rubric_unmet'): criterion['met']=resolved[criterion['id']]=='rubric_met'
        rubric['score']=max(0,min(1,sum(c['points'] for c in rubric['criteria'] if c['met'])/max(1,sum(max(0,c['points']) for c in rubric['criteria']))))
    scored=grade(task,replay,key,rubric=rubric,review_overrides=decisions)
    trace['final_verification']=public_verification(scored);trace['final_verification_enc']=seal(scored)
    store.ehr_update('ehr_rollouts',{'rollout_id':rollout_id},{'status':'final','final_reward':scored['reward'],
                    'hard_fail':int(scored['hard_fail']),'hard_fail_reason':scored.get('hard_fail_reason'),'trajectory_json':dumps(trace),'updated_at':audit_now()},_connection=connection)
    for cp in scored['checkpoints']:
        store.ehr_update('ehr_checkpoints',{'rollout_id':rollout_id,'kind':cp['kind']},{'score':cp['score'],'verdict':cp['verdict'],
                        'items_json':dumps(public_verification(cp['items'])),'grader':'physician' if decisions else 'code'},_connection=connection)


def reveal_outcome(review_id,user_id,*,store=None):
    store=store or get_store(); require_live_review_assignment(store,review_id,user_id,submitted=True)
    row=store.ehr_get('ehr_reviews',review_id=review_id)
    if row['trigger']!='outcome_flag': raise ValueError('this review has no outcome phase')
    visit,chart=_chart(store,row['visit_id'])
    # Convert future resources to the same synthetic calendar before display.
    from .visit_compiler import synthetic_resources
    original=json.loads(chart['resources_json']); future=unseal(visit['outcome_enc'])['resources']
    patient=next(r for r in original if r['resourceType']=='Patient')
    resources,_,_=synthetic_resources([patient]+future,visit['offset_days'],int(digest(visit['visit_id'])[:8],16),0,reference_resources=original)
    return {'review_id':review_id,'outcome':resources}


def record_reflection(review_id,user_id,reflection,*,store=None):
    store=store or get_store();reveal_outcome(review_id,user_id,store=store)
    if reflection.get('change') not in ('no_change','a_was_wrong','b_was_wrong','proposed_plan_was_wrong') or len(str(reflection.get('note','')).strip())<20: raise ValueError('reflection requires a choice and substantive note')
    # An append-only audit event preserves the ex-ante verdict byte-for-byte.
    store.log_event(entity_type='ehr_review',entity_id=review_id,event_type='ehr_outcome_reflection',actor=user_id,payload=reflection)
    return {'recorded':True}


def reassign_expired(store=None):
    store=store or get_store(); now=audit_now(); count=0
    with store._conn() as conn:
        store._immediate(conn)
        for row in store.ehr_all('ehr_review_assignments',limit=10000,_connection=conn):
            if row['status'] in LIVE and row['expires_at']<=now:
                count+=store.ehr_update('ehr_review_assignments',{'review_assignment_id':row['review_assignment_id'],'status':row['status']},{'status':'expired'},_connection=conn)
    failed=[]
    for row in store.ehr_all('ehr_reviews',limit=10000):
        if row['status'] in CLOSED: continue
        # One review on a chart that left 'built' must not stop the hourly sweep.
        try: select_reviewers(row['review_id'],store=store)
        except Exception as exc:  # noqa: BLE001 - logged; the sweep continues
            failed.append(row['review_id']); logger.warning('EHR review %s not re-offered: %s',row['review_id'],exc)
    return {'expired':count,'reoffer_failed':failed}


def review_items(row):
    value=json.loads(row['items_json'])
    return unseal(value['sealed']) if isinstance(value,dict) and 'sealed' in value else value


def _resolve_cached_pending(store,conn,visit_id,now):
    for row in store.ehr_all('ehr_reviews',visit_id=visit_id,_connection=conn):
        if row['trigger']!='disagreement' or row['status'] in CLOSED: continue
        items=review_items(row); mapped={}
        for item in items:
            cache=store.ehr_get('ehr_verdict_cache',cache_key=digest([visit_id,item['signature']]),_connection=conn)
            if cache: mapped[item['item_id']]=cache['verdict']
        if len(mapped)!=len(items): continue
        store.ehr_update('ehr_reviews',{'review_id':row['review_id']},{'status':'resolved','reason':'cached_resolution',
            'resolution_json':dumps({'items':mapped,'agreement_pairs':[]}), 'resolved_at':now},_connection=conn)
        for assignment in store.ehr_all('ehr_review_assignments',review_id=row['review_id'],_connection=conn):
            if assignment['status'] in LIVE: store.ehr_update('ehr_review_assignments',{'review_assignment_id':assignment['review_assignment_id']},{'status':'cancelled'},_connection=conn)


def _key_audit_vote(store,row,audit,connection):
    if audit.get('status')=='correct': return {'key-audit':'key_correct'}
    if audit.get('status')!='corrected' or not isinstance(audit.get('replacement_key'),dict): raise ValueError('Key audit must confirm or provide a complete corrected key')
    visit,chart=_chart(store,row['visit_id'],connection)
    visit_meta=next(v for v in json.loads(chart['extraction_json'])['visits'] if v['encounter_ref']==visit['encounter_ref'])
    worksheet='\n'.join(document_text(r) for r in json.loads(chart['resources_json']) if r['id'] in visit_meta['document_ids'])
    from .worksheet_extract import grounded_key,SCHEMA
    candidate=copy.deepcopy({k:v for k,v in audit['replacement_key'].items() if k in SCHEMA['properties']})
    original=unseal(visit['key_enc'])
    structured={i['item_id']:i for i in original.get('med_changes',[]) if i.get('source')=='structured'}
    retained=[]; extracted=[]
    from .terminology import medication_span_supported
    for item in candidate.get('med_changes',[]):
        if item.get('source')=='structured' or item.get('item_id') in structured:
            if structured.get(item.get('item_id'))!=item: raise ValueError('Structured source decisions must remain unchanged; remove a disputed item with a rationale instead')
            retained.append(copy.deepcopy(item))
        else:
            if not medication_span_supported(item): raise ValueError('Corrected medication decisions require a quote supporting the drug, action and dose')
            extracted.append(item)
    candidate['med_changes']=extracted
    replacement=grounded_key(candidate,worksheet)
    if replacement['extraction']['dropped']: raise ValueError('Corrected decisions must cite exact worksheet spans')
    replacement['med_changes'].extend(retained)
    audit['replacement_key']=replacement
    return {'key-audit':'key_corrected:'+digest(replacement)}


def verdict_data(row):
    value=json.loads(row['verdict_json'])
    return unseal(value['sealed']) if 'sealed' in value else value
