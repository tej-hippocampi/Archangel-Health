"""Pre-slice before runtime construction; seal reference decisions and outcomes."""
from __future__ import annotations
import base64
import copy
import json
import re
from datetime import timedelta

from asclepius.store import get_store
from .common import digest,dumps,identifier,ext,extension,document_text,resource_ref,subject,validate
from .constants import ANCHOR,DECISION_INSTANT,audit_now,settings,OFFSET_URL
from .identities import identity
from .sealed import seal,unseal


def split_for(chart_id):
    bucket=int(digest(chart_id)[:8],16)%100
    return 'train' if bucket<70 else 'dev' if bucket<85 else 'heldout'


def _prune_references(value,visible):
    if isinstance(value,dict):
        if 'reference' in value and value['reference'] not in visible:
            return None
        return {k:result for k,v in value.items() if (result:=_prune_references(v,visible)) is not None}
    if isinstance(value,list):
        return [result for v in value if (result:=_prune_references(v,visible)) is not None]
    return value


def slice_resources(resources,day):
    """Relative-time boundary. Never retain the original resource collection."""
    out=[]
    for original in resources:
        kind=original['resourceType']; offset=ext(original)
        same_day=offset==day and kind in ('Observation','Encounter')
        if kind!='Patient' and (not isinstance(offset,int) or offset>day or (offset==day and not same_day)):
            continue
        if kind=='Observation' and same_day:
            ordered={x['id']:ext(x) for x in resources if x['resourceType']=='ServiceRequest'}
            if any(ordered.get(x.get('reference','').split('/')[-1],day)>=day for x in original.get('basedOn',[])):
                continue
        item=copy.deepcopy(original)
        if kind=='Encounter' and same_day:
            item['status']='in-progress'
            for field in ('reasonCode','type','diagnosis','serviceType','reasonReference'):
                item.pop(field,None)
        if kind=='MedicationRequest':
            stop=ext(item,'stopOffset')
            if stop is None or stop>=day:
                item['status']='active'
                for field in ('statusReason','reasonCode','reasonReference'):
                    item.pop(field,None)
                item['extension']=[e for e in item.get('extension',[]) if e['url']!=OFFSET_URL.replace('offsetDays','stopOffset')]
        if kind=='Condition':
            item.pop('abatementDateTime',None); item.pop('abatementPeriod',None)
            # Each assertion has its own authored offset; preserve that historical status.
        if kind=='ServiceRequest':
            item['status']='completed' if any(ext(r) is not None and ext(r)<day and {'reference':resource_ref(item)} in r.get('basedOn',[]) for r in resources if r['resourceType']=='Observation') else 'active'
        if kind=='Appointment':
            item['status']='booked'
        out.append(item)
    visible={resource_ref(r) for r in out}
    return [_prune_references(r,visible) for r in out]


def assert_fhir_slice(resources,day):
    visible={resource_ref(r) for r in resources}
    for r in resources:
        kind=r['resourceType']; offset=ext(r)
        if kind!='Patient' and (not isinstance(offset,int) or offset>day or (offset==day and kind not in ('Observation','Encounter'))):
            raise ValueError('temporal_slice_failed')
        if kind=='MedicationRequest' and r.get('status')!='active' and (ext(r,'stopOffset') is None or ext(r,'stopOffset')>=day):
            raise ValueError('future_medication_status')
        if kind=='Encounter' and offset==day and any(k in r for k in ('reasonCode','type','diagnosis','serviceType')):
            raise ValueError('same_day_encounter_leak')
        def walk(node):
            if isinstance(node,dict):
                if 'reference' in node and node['reference'] not in visible:
                    raise ValueError('excluded_resource_reference')
                for field,value in node.items():
                    if field in ('authoredOn','recordedDate','effectiveDateTime','birthDate','date','abatementDateTime','issued','occurrenceDateTime','start','end') and isinstance(value,str) and re.search(r'\d{4}-\d{2}-\d{2}',value):
                        raise ValueError('absolute_date_in_canonical_slice')
                    walk(value)
            elif isinstance(node,list):
                for value in node: walk(value)
        walk(r)


def assert_no_worksheet_leak(resources,worksheet):
    tokens=re.findall(r'\w+',worksheet.lower())
    sequences={' '.join(tokens[i:i+8]) for i in range(max(0,len(tokens)-7))}
    texts=[document_text(r) if r['resourceType']=='DocumentReference' else dumps(r) for r in resources]
    for text in texts:
        visible=re.findall(r'\w+',text.lower())
        if any(' '.join(visible[i:i+8]) in sequences for i in range(max(0,len(visible)-7))):
            raise ValueError('worksheet_text_overlap')


def synthetic_resources(resources,day,seed,index,*,family=None,birth_year=None,gender=None,reference_resources=()):
    out=copy.deepcopy(resources)
    patient=next(r for r in out if r['resourceType']=='Patient')
    generated=identity(seed,index,ext(patient,'ageBand','60-69'),gender or patient.get('gender','unknown'),family=family,birth_year=birth_year)
    patient.update(generated)
    id_map={resource_ref(r):r['resourceType']+'/'+identifier('p' if r['resourceType']=='Patient' else 'r',[seed,index,resource_ref(r)]) for r in [*reference_resources,*out]}
    def rewrite(value):
        if isinstance(value,dict):
            return {k:id_map.get(v,v) if k=='reference' else rewrite(v) for k,v in value.items()}
        if isinstance(value,list): return [rewrite(v) for v in value]
        return value
    for r in out:
        old=resource_ref(r); offset=ext(r)
        r['id']=id_map[old].split('/')[1]
        if isinstance(offset,int):
            at=(ANCHOR+timedelta(days=offset-day)).isoformat()+'T09:00:00-08:00'
            field={'Observation':'effectiveDateTime','DocumentReference':'date','MedicationRequest':'authoredOn',
                   'Condition':'recordedDate','ServiceRequest':'authoredOn','DiagnosticReport':'effectiveDateTime'}.get(r['resourceType'])
            if field: r[field]=at
            if r['resourceType']=='Encounter': r['period']={'start':at}
            if r['resourceType']=='Appointment':
                due=ext(r,'dueInterval',0)
                r['start']=(ANCHOR+timedelta(days=offset-day+due)).isoformat()+'T09:00:00-08:00'
                r['end']=(ANCHOR+timedelta(days=offset-day+due)).isoformat()+'T09:30:00-08:00'
            if r['resourceType']=='DocumentReference':
                text=document_text(r)
                text=re.sub(r'Day\s*([+-]?\d+)',lambda m:(ANCHOR+timedelta(days=int(m.group(1))-day)).isoformat(),text)
                r['content']=[{'attachment':{'contentType':'text/plain','data':base64.b64encode(text.encode()).decode()}}]
        r['extension']=[e for e in r.get('extension',[]) if e['url'].endswith('ageBand')]
        if not r['extension']: r.pop('extension',None)
    out=rewrite(out)
    for r in out: validate(r)
    return out,next(r for r in out if r['resourceType']=='Patient'),id_map


def waive_audit(visit_id,reason,actor,*,store=None):
    if len(reason.strip())<20: raise ValueError('audit waiver requires a substantive reason')
    store=store or get_store()
    with store._conn() as conn:
        store._immediate(conn)
        store.ehr_update('ehr_visits',{'visit_id':visit_id},{'key_audit':'waived'},_connection=conn)
        for review in store.ehr_all('ehr_reviews',visit_id=visit_id,trigger='key_audit',_connection=conn):
            if review['status'] not in ('resolved','unresolved','cancelled'):
                store.ehr_update('ehr_reviews',{'review_id':review['review_id']},{'status':'cancelled','reason':'admin_audit_waiver'},_connection=conn)
                for assignment in store.ehr_all('ehr_review_assignments',review_id=review['review_id'],_connection=conn):
                    if assignment['status'] in ('offered','claimed'): store.ehr_update('ehr_review_assignments',{'review_assignment_id':assignment['review_assignment_id']},{'status':'cancelled'},_connection=conn)
        conn.execute('INSERT INTO events(entity_type,entity_id,event_type,actor,occurred_at,payload_json) VALUES (?,?,?,?,?,?)',
                     ('ehr_visit',visit_id,'ehr_key_audit_waived',actor,audit_now(),dumps({'reason':reason})))


def outcome_window(resources,day,next_day):
    """Materialize the follow-up as of its end, including dated status changes."""
    if next_day is None: return {'resources':[]}
    visible=slice_resources(resources,next_day+1)
    selected=[r for r in visible if ext(r) is not None and day<ext(r)<=next_day]
    for medication in visible:
        stop=ext(medication,'stopOffset')
        if medication['resourceType']!='MedicationRequest' or not isinstance(stop,int) or not day<stop<=next_day:
            continue
        event=copy.deepcopy(medication)
        event['id']=identifier('med-status',[medication['id'],stop])
        event['priorPrescription']={'reference':resource_ref(medication)}
        event['extension']=[e for e in event.get('extension',[]) if e['url']!=OFFSET_URL]+[extension('offsetDays',stop)]
        selected.append(validate(event))
    return {'resources':selected}


def _ckd_stage(resources):
    from .terminology import GROUPS
    from .calculators import calculate
    rows=[r for r in resources if r['resourceType']=='Observation'
          and any(c.get('code') in GROUPS['eGFR'] for c in r.get('code',{}).get('coding',[]))
          and isinstance(r.get('valueQuantity',{}).get('value'),(int,float))]
    if not rows: return None
    latest=max(rows,key=lambda r:(ext(r),r['id']))
    return calculate('ckd_stage',{'egfr':latest['valueQuantity']['value']})['value']


def decoy_peers(peers,day,seed,target,target_resources,*,third=False):
    """Select compatible source charts before assigning synthetic demographics."""
    candidates=[]
    for peer in sorted(peers,key=lambda c:digest([seed,c['chart_id']])):
        visits=json.loads(peer['extraction_json'])['visits']
        chosen=min(visits,key=lambda v:abs(v['offset_days']-day))
        visible=slice_resources(json.loads(peer['resources_json']),chosen['offset_days'])
        patient=next(r for r in visible if r['resourceType']=='Patient')
        candidates.append((peer,chosen,visible,patient))
    compatible=[]
    for row in candidates:
        patient=row[3]
        if patient.get('gender','unknown')!=target['gender']: continue
        try: identity(seed,2,ext(patient,'ageBand','60-69'),patient.get('gender','unknown'),birth_year=int(target['birthDate'][:4]))
        except ValueError: continue
        compatible.append(row)
    if not compatible: raise ValueError('insufficient_age_sex_matched_decoys')
    stage=_ckd_stage(target_resources)
    for second in compatible:
        others=[r for r in candidates if r[0]['chart_id']!=second[0]['chart_id']]
        third_row=next((r for r in others if stage is not None and _ckd_stage(r[2])==stage),None) if third else None
        if third and third_row is None: continue
        first=next((r for r in others if r is not third_row),None)
        if first is not None: return [first,second]+([third_row] if third else [])
    raise ValueError('insufficient_ckd_stage_matched_decoys' if third else 'insufficient_same_split_decoys')


def compile_chart(chart_id,*,store=None,dry_run=False):
    store=store or get_store(); chart=store.ehr_get('ehr_charts',chart_id=chart_id)
    if not chart or chart['status']!='built': raise ValueError('chart is not built')
    report={'chart_id':chart_id,'built':[],'pending':[],'excluded':[]}
    resources=json.loads(chart['resources_json']); extraction=json.loads(chart['extraction_json']); split=split_for(chart_id)
    peers=[c for c in store.ehr_all('ehr_charts',upload_id=chart['upload_id'],status='built') if c['chart_id']!=chart_id and split_for(c['chart_id'])==split]
    upload_charts=sorted(store.ehr_all('ehr_charts',upload_id=chart['upload_id'],status='built'),key=lambda c:c['created_at'])
    rank=0
    for other in upload_charts:
        if other['chart_id']==chart_id: break
        rank+=other['n_visits']
    for index,visit in enumerate(extraction['visits']):
        day=visit['offset_days']; visit_id=identifier('visit',[chart_id,visit['encounter_ref']]); key=unseal(visit['key_enc'])
        old=store.ehr_get('ehr_visits',visit_id=visit_id)
        if old:
            from .harness import effective_key
            key=effective_key(old,store=store)
        sampled=rank+index<20 or int(digest(visit_id)[:8],16)%100<5 or visit['key_confidence']<settings().key_min_confidence or bool(visit['key_conflict'])
        audit=old['key_audit'] if old else ('pending' if sampled else 'not_sampled')
        row={'visit_id':visit_id,'chart_id':chart_id,'encounter_ref':visit['encounter_ref'],'visit_index':index,'offset_days':day,
             'has_next_visit':int(index+1<len(extraction['visits'])),'key_enc':visit['key_enc'],
             'key_confidence':visit['key_confidence'],'key_conflict':visit['key_conflict'],'key_audit':audit,'created_at':audit_now()}
        next_day=extraction['visits'][index+1]['offset_days'] if row['has_next_visit'] else None
        row['outcome_enc']=seal(outcome_window(resources,day,next_day))
        if not old and not dry_run: store.ehr_insert('ehr_visits',row)
        if audit=='pending' and not dry_run:
            from .reviews import create_review
            from .items import key_items
            create_review(visit_id,'key_audit',[{'item_id':i['item_id'],'key':i,'actual':i} for i in key_items(key)],scope='visit',store=store)
        reason=None
        if audit not in ('passed','waived','not_sampled'): reason='key_audit_pending'
        elif audit!='passed' and (visit['key_confidence']<settings().key_min_confidence or visit['key_conflict']): reason='key_confidence_or_conflict'
        elif not key['med_changes'] and not key['orders'] and not key['referrals'] and not key.get('follow_up'): reason='no_gradable_action'
        elif len(peers)<2: reason='insufficient_same_split_decoys'
        if reason:
            destination='excluded' if reason=='no_gradable_action' else 'pending'
            report[destination].append({'visit_id':visit_id,'reason':reason})
            if destination=='excluded' and not dry_run: store.ehr_update('ehr_visits',{'visit_id':visit_id},{'status':'excluded','exclusion_reason':reason})
            continue
        try:
            visible=slice_resources(resources,day); assert_fhir_slice(visible,day)
            assert_legacy_slice(visible,day)
            worksheet='\n'.join(document_text(r) for r in resources if r['resourceType']=='DocumentReference' and r['id'] in visit['document_ids'])
            assert_no_worksheet_leak(visible,worksheet)
            seed=int(digest(visit_id)[:8],16)
            snapshot,target,id_map=synthetic_resources(visible,day,seed,0)
            selected=decoy_peers(peers,day,seed,target,visible,third=len(upload_charts)>=20)
            for i,(_,chosen,pr,_) in enumerate(selected,1):
                assert_fhir_slice(pr,chosen['offset_days'])
                decoy,_,_=synthetic_resources(pr,chosen['offset_days'],seed,i,
                    family=target['name'][0]['family'] if i==1 else None,
                    birth_year=int(target['birthDate'][:4]) if i==2 else None)
                snapshot.extend(decoy)
        except ValueError as exc:
            report['excluded'].append({'visit_id':visit_id,'reason':str(exc)})
            if not dry_run: store.ehr_update('ehr_visits',{'visit_id':visit_id},{'status':'excluded','exclusion_reason':str(exc)})
            continue
        task_id=identifier('ehrt',[visit_id,settings().env_version])
        instruction=f"You are the nephrologist at an outpatient clinic. Today is Monday 3 March 2031. You are seeing {target['name'][0]['text']} (MRN {target['identifier'][0]['value']}, DOB {target['birthDate']}) for a scheduled follow-up visit. Use the EHR tools to review the chart, make today's clinical decisions, place any orders, and write today's visit note. Only act on this patient. Call finish_visit when you are done."
        task={'task_id':task_id,'visit_id':visit_id,'env_version':settings().env_version,'split':split,'seed':seed,
              'snapshot_json':dumps({'resourceType':'Bundle','type':'collection','identifier':{'value':target['id']},
                  'meta':{'tag':[{'system':'https://archangel.health/source-country','code':country} for country in sorted({chart['source_country'],*(p[0]['source_country'] for p in selected)})]},
                  'entry':[{'resource':r} for r in snapshot]}),
              'instruction':instruction,'budget_tool_calls':settings().budget_tool_calls,'tags_json':dumps(['F1','F2','F3','action' if key['med_changes'] or key['orders'] or key['referrals'] else 'no_action']),
              'created_at':audit_now()}
        from .harness import sanity
        baseline=sanity(task,key)
        if not baseline['passed']:
            report['excluded'].append({'visit_id':visit_id,'reason':'baseline_sanity_failed'})
            if not dry_run: store.ehr_update('ehr_visits',{'visit_id':visit_id},{'status':'excluded','exclusion_reason':'baseline_sanity_failed'})
            continue
        if baseline['doctor_flagged'] and not dry_run:
            store.ehr_update('ehr_visits',{'visit_id':visit_id},{'doctor_flagged':1})
            from .reviews import create_review
            create_review(visit_id,'safety',[{'item_id':identifier('finding',[i['rule_id'],i.get('item_id'),n]),'key':{'type':'safety','text':i['reason']},'actual':None,'rule':i} for n,i in enumerate(baseline['oracle']['safety'])],scope='visit',store=store)
        if not dry_run and not store.ehr_get('ehr_tasks',task_id=task_id):
            store.ehr_insert('ehr_tasks',task)
            store.ehr_update('ehr_visits',{'visit_id':visit_id},{'status':'ready'})
        if not dry_run:
            from .probes import compile_probes
            compile_probes(task,store=store)
        report['built'].append({'task_id':task_id,'visit_id':visit_id,'split':split})
    report['balance']=balance_split(split,store=store,dry_run=dry_run)
    return report


def assert_legacy_slice(resources,day):
    """Run the established ClinicalCase temporal gate on the rebased FHIR view too."""
    from asclepius.real_cases import assert_temporal_split
    mapping={'Observation':'lab_panels','MedicationRequest':'medications','Condition':'problem_list','DocumentReference':'notes'}
    view={k:[] for k in mapping.values()}
    for r in resources:
        if r['resourceType'] in mapping:
            view[mapping[r['resourceType']]].append({'collected_offset_days':ext(r)-day})
    assert_temporal_split(view)


def balance_split(split,*,store=None,dry_run=False):
    """Deterministic downsampling changes status only; every task is preserved."""
    store=store or get_store()
    tasks=store.ehr_all('ehr_tasks',split=split,task_kind='visit')
    eligible=[]
    for task in tasks:
        visit=store.ehr_get('ehr_visits',visit_id=task['visit_id'])
        chart=store.ehr_get('ehr_charts',chart_id=visit['chart_id']) if visit else None
        if chart and chart['status']=='built' and visit['status']=='ready': eligible.append(task)
    classes={key:sorted([t for t in eligible if key in json.loads(t.get('tags_json') or '[]')],key=lambda t:digest(t['task_id'])) for key in ('action','no_action')}
    minority=min(len(v) for v in classes.values())
    cap=int(minority*1.5)
    retained={t['task_id'] for rows in classes.values() for t in rows[:cap]}
    dropped=[t['task_id'] for t in eligible if t['task_id'] not in retained]
    if not dry_run:
        for task in eligible:
            status='ready' if task['task_id'] in retained else 'retired'
            if status!=task['status']: store.ehr_update('ehr_tasks',{'task_id':task['task_id']},{'status':status})
    return {'split':split,'action':len(classes['action']),'no_action':len(classes['no_action']),
            'retained':len(retained),'dropped':dropped,'balanced':bool(retained),
            'warning':None if retained else 'Both action and no-action visits are required before evaluation or export.'}
