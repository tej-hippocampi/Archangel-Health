"""Normalize keys and actual persisted end states; maximum-weight item matching."""
import re
from datetime import datetime
from .common import subject,ext
from .terminology import drug,daily_dose,daily_amount,lab_group,GROUPS,normalize,same_titration_step
from .constants import DECISION_INSTANT


def medication_dose(resource):
    amount=medication_amount(resource)
    return amount['value'] if amount and amount['unit']=='mg' else None


def medication_amount(resource):
    instructions=resource.get('dosageInstruction') or [{}]; d=instructions[0]
    quantity=(d.get('doseAndRate') or [{}])[0].get('doseQuantity') or {}
    text=f"{quantity['value']} {quantity.get('unit','')}" if 'value' in quantity else d.get('text','')
    frequency=d.get('timing',{}).get('code',{}).get('text') or d.get('text','')
    return daily_amount(text,frequency)


def concept_code(value):
    return next((x.get('code') for x in (value or {}).get('coding',[]) if x.get('code')),None)


def key_items(key):
    items=[]
    for category in ('assessments','med_changes','orders','referrals'):
        for source in key.get(category,[]):
            item=dict(source); item['type']={'assessments':'assessment','med_changes':'med','orders':source.get('kind','other'),'referrals':'referral'}[category]
            if item['type']=='med':
                amount=daily_amount(item.get('to_dose'))
                item['ingredient']=drug(item['drug'])['name']; item['dose']=amount['value'] if amount else None
                item['dose_unit']=amount['unit'] if amount else None
                if item.get('to_dose') and item['dose'] is None: item['not_gradable']=True
            if item['type']=='assessment' and not item.get('icd10'): item['not_gradable']=True
            if item['type']=='lab': item['group']=lab_group(item.get('loinc_group') or item.get('text',''))
            if item['type']=='lab' and not item['group'] and not item.get('codes'): item['not_gradable']=True
            if item['type'] not in ('assessment','med','lab','imaging','referral'): item['not_gradable']=True
            items.append(item)
    for field,kind in [('follow_up','follow_up'),('escalation','escalation')]:
        if key.get(field): items.append({**key[field],'type':kind})
    for index,item in enumerate(items): item.setdefault('item_id',f'key-{index}')
    return items


def actual_items(snapshot,overlay,target):
    initial={r['id']:r for r in snapshot if subject(r)==target}
    writes={r['id']:r for r in overlay if subject(r)==target}
    final=initial | writes; items=[]
    baseline=[r for r in initial.values() if r['resourceType']=='MedicationRequest' and r.get('status')=='active']
    new_meds=[r for r in writes.values() if r['resourceType']=='MedicationRequest' and r.get('status')=='active' and r['id'] not in initial]
    replaced=set()
    for r in new_meds:
        name=drug(r['medicationCodeableConcept']['text'])['name']
        ancestor=r; visited=set()
        while ancestor.get('priorPrescription'):
            aid=ancestor['priorPrescription']['reference'].split('/')[-1]
            if aid in visited: break
            visited.add(aid); ancestor=final.get(aid,{})
        old=initial.get(ancestor.get('id'))
        if not old:
            # Unlinked replacement still counts as replacement if the old is stopped.
            old=next((m for m in baseline if drug(m['medicationCodeableConcept']['text'])['name']==name and final[m['id']]['status']!='active'),None)
        amount=medication_amount(r); previous=medication_amount(old) if old else None
        dose=amount['value'] if amount else None
        before=previous['value'] if previous and amount and previous['unit']==amount['unit'] else None
        action='start'
        if old and final[old['id']].get('status')=='active': old=None
        if old:
            replaced.add(old['id']); action='increase' if before is not None and dose is not None and dose>before else 'decrease' if before is not None and dose is not None and dose<before else 'change'
        items.append({'type':'med','item_id':r['id'],'ingredient':name,'drug':r['medicationCodeableConcept']['text'],'action':action,'dose':dose,'dose_unit':amount['unit'] if amount else None,'resource':r})
    for old in baseline:
        now=final[old['id']]
        if old['id'] in writes and old['id'] not in replaced and now.get('status') in ('stopped','on-hold','cancelled'):
            items.append({'type':'med','item_id':old['id'],'ingredient':drug(old['medicationCodeableConcept']['text'])['name'],'drug':old['medicationCodeableConcept']['text'],
                          'action':'hold' if now['status']=='on-hold' else 'stop','dose':None,'resource':now})
    anchor=datetime.fromisoformat(DECISION_INSTANT)
    for r in writes.values():
        kind=r['resourceType']; item={'item_id':r['id'],'resource':r}
        if kind=='ServiceRequest' and r.get('status')=='active':
            category=concept_code((r.get('category') or [{}])[0]) or (r.get('category') or [{}])[0].get('text','other')
            item.update(type=category,text=r.get('code',{}).get('text',''))
            if category=='lab':
                item.update(group=lab_group(item['text']) or ext(r.get('code',{}),'labGroup'),codes=[c['code'] for c in r.get('code',{}).get('coding',[])])
            if category=='referral': item['specialty']=concept_code(r.get('code')) or item['text']
            if r.get('occurrenceDateTime'): item['timing_days']=(datetime.fromisoformat(r['occurrenceDateTime'])-anchor).days
        elif kind=='Appointment' and r.get('status')=='booked': item.update(type='follow_up',interval_days=(datetime.fromisoformat(r['start'])-anchor).days)
        elif kind=='Condition': item.update(type='assessment',icd10=concept_code(r.get('code')),text=r.get('code',{}).get('text',''))
        elif kind=='Flag' and r.get('status')=='active': item.update(type='escalation',action=r.get('code',{}).get('text'))
        else: continue
        items.append(item)
    return items


def compatible(expected,actual):
    if expected['type']!=actual['type']: return 0.0
    kind=expected['type']
    if kind=='med':
        if expected['ingredient']!=actual['ingredient'] or expected['action']!=actual['action']: return 0.0
        if expected.get('dose') is None: return 1.0 if expected['action'] in ('hold','stop') else 0.0
        return float(actual.get('dose') is not None and actual.get('dose_unit','mg')==expected.get('dose_unit','mg') and
            (abs(actual['dose']-expected['dose'])<=0.25*expected['dose'] or same_titration_step(expected['ingredient'],expected['dose'],actual['dose'],expected.get('dose_unit') or 'mg')))
    if kind=='lab':
        want=GROUPS.get(expected.get('group'),set(expected.get('codes') or []))
        got=GROUPS.get(actual.get('group'),set(actual.get('codes') or []))
        if not want or not want<=got: return 0.0
        timing=expected.get('timing_days')
        return float(timing is None or (actual.get('timing_days') is not None and abs(actual['timing_days']-timing)<=max(3,0.5*timing)))
    if kind=='referral': return float(expected.get('specialty')==actual.get('specialty'))
    if kind=='imaging': return float(normalize(expected.get('text',''))==normalize(actual.get('text','')))
    if kind=='follow_up': return float(abs(expected['interval_days']-actual['interval_days'])<=expected.get('tolerance_days',max(7,0.25*expected['interval_days'])))
    if kind=='escalation': return 1.0 if expected['action']==actual['action'] else 0.5
    if kind=='assessment':
        a,b=expected.get('icd10') or '',actual.get('icd10') or ''
        if not a or a[:3]!=b[:3]: return 0.0
        if a.startswith('N18.'):
            if a==b: return 1.0
            try: return 0.5 if abs(int(a[4])-int(b[4]))==1 else 0.0
            except (IndexError,ValueError): return 0.0
        return 1.0
    return 0.0


def maximum_match(expected,actual):
    """Rectangular Hungarian assignment with a private zero-credit dummy per key."""
    n=len(expected); m=len(actual)+n
    if not n: return [],list(actual)
    costs=[[-compatible(a,b) for b in actual]+[0.0]*n for a in expected]
    u=[0.0]*(n+1); v=[0.0]*(m+1); p=[0]*(m+1); way=[0]*(m+1)
    for i in range(1,n+1):
        p[0]=i; j0=0; low=[float('inf')]*(m+1); used=[False]*(m+1)
        while True:
            used[j0]=True; i0=p[j0]; delta=float('inf'); j1=0
            for j in range(1,m+1):
                if not used[j]:
                    cur=costs[i0-1][j-1]-u[i0]-v[j]
                    if cur<low[j]: low[j]=cur; way[j]=j0
                    if low[j]<delta: delta=low[j]; j1=j
            for j in range(m+1):
                if used[j]: u[p[j]]+=delta; v[j]-=delta
                else: low[j]-=delta
            j0=j1
            if p[j0]==0: break
        while True:
            j1=way[j0]; p[j0]=p[j1]; j0=j1
            if j0==0: break
    assignments={p[j]-1:j-1 for j in range(1,m+1) if p[j]}; consumed=set(); matched=[]
    for i,key in enumerate(expected):
        j=assignments[i]; credit=0 if j>=len(actual) else -costs[i][j]
        if credit>0: consumed.add(j)
        matched.append({'key':key,'actual':actual[j] if credit>0 else None,'credit':credit,'status':'matched' if credit>0 else 'missed'})
    return matched,[a for j,a in enumerate(actual) if j not in consumed]
