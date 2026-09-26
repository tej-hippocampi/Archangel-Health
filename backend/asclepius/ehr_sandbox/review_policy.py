"""Portable, identity-free physician decision matching shared by both graders."""
from .common import digest
from .terminology import normalize,drug

def render_plan_item(item, *, include_codes=False):
    """Single rendering path strips source IDs, codes, spans, confidence and style."""
    if not item: return {'decision':'No corresponding action'}
    kind=item.get('type') or item.get('kind','other')
    text=item.get('ingredient') or item.get('drug') or item.get('group') or item.get('loinc_group') or item.get('specialty') or item.get('text') or kind
    if kind=='med': text=drug(text)['name']
    result={'decision':normalize(text),'action':item.get('action') or kind}
    if item.get('dose') is not None:
        if item.get('dose_unit','mg')=='mg': result['daily_dose_mg']=item['dose']
        else: result.update(daily_dose=item['dose'],dose_unit=item['dose_unit'])
    if kind=='assessment':
        ckd={'N18.1':'CKD stage 1','N18.2':'CKD stage 2','N18.3':'CKD stage 3',
             'N18.30':'CKD stage 3','N18.31':'CKD stage 3a','N18.32':'CKD stage 3b',
             'N18.4':'CKD stage 4','N18.5':'CKD stage 5','N18.6':'End stage kidney disease'}
        result['diagnosis']=ckd.get(item.get('icd10'),normalize(item.get('text','')))
        if include_codes: result['diagnosis_code']=item.get('icd10')
    if kind=='lab' and include_codes and item.get('codes'): result['test_codes']=sorted(item['codes'])
    if item.get('rule'): result['safety_rule']=item['rule']
    if item.get('timing_days') is not None: result['timing_days']=item['timing_days']
    if item.get('interval_days') is not None: result['interval_days']=item['interval_days']
    return result


def signature(item):
    return digest({'doctor':render_plan_item(item.get('key'),include_codes=True),'agent':render_plan_item(item.get('actual'),include_codes=True),'rule':item.get('rule')})


def _disputes(verification):
    rows=[]
    for cp in verification.get('checkpoints',[]):
        if cp['kind'] not in ('act','reason'): continue
        missed=[x for x in cp['items'] if x.get('status')=='missed']
        extras=[x for x in cp['items'] if x.get('status') in ('extra','extra_unsafe','conflict')]
        used=set()
        for m in missed:
            key=m['key']; candidate=next((x for x in extras if x['actual']['item_id'] not in used and x['actual']['type']==key['type']
                and (key['type']!='med' or x['actual'].get('ingredient')==key.get('ingredient'))),None)
            actual=candidate['actual'] if candidate else None
            if actual: used.add(actual['item_id'])
            rows.append({'item_id':key['item_id'],'key':key,'actual':actual,'checkpoint':cp['kind']})
        for x in extras:
            if x['actual']['item_id'] not in used: rows.append({'item_id':'extra-'+x['actual']['item_id'],'key':None,'actual':x['actual'],'checkpoint':cp['kind']})
    for item in rows: item['signature']=signature(item)
    return rows




def overrides(verification,policy):
    result={};cached={p['signature']:p['verdict'] for p in policy.get('decisions',[])}
    for item in _disputes(verification):
        verdict=cached.get(item['signature'])
        if verdict:
            result[item['item_id']]={'verdict':verdict,'key_id':(item.get('key') or {}).get('item_id'),
                                    'actual_id':(item.get('actual') or {}).get('item_id')}
    for finding in verification.get('safety',[]):
        for decision in policy.get('safety',[]):
            if decision['rule_id']==finding['rule_id'] and bool(finding.get('fingerprint')) and decision.get('fingerprint')==finding.get('fingerprint'):
                result['safety-'+digest(finding)]={**decision}
    return result
