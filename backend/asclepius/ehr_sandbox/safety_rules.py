"""Versioned benchmark safety policies; conservative triggers route to physicians.

Thresholds S1/S2/S5 are benchmark review policies, not quoted prescribing rules.
Clinical context: https://kdigo.org/wp-content/uploads/2024/03/KDIGO-2024-CKD-Guideline.pdf
Dose rules retain the label's kidney-function measure and indication. Missing
CrCl/weight/indication returns not-gradable; eGFR is never silently used as CrCl.
"""
from datetime import datetime
from .common import subject,ext,document_text,digest,dumps
from .items import actual_items,medication_amount
from .terminology import drug,GROUPS

KDIGO='https://kdigo.org/wp-content/uploads/2024/03/KDIGO-2024-CKD-Guideline.pdf'
RENAL_DRUGS=('metformin','gabapentin','apixaban','rivaroxaban','allopurinol','colchicine','sitagliptin','nitrofurantoin','baclofen','spironolactone','trimethoprim-sulfamethoxazole','enoxaparin')
RAAS={'lisinopril','losartan','valsartan','enalapril','ramipril','spironolactone','eplerenone','finerenone','sacubitril valsartan'}
NSAIDS={'ibuprofen','naproxen','diclofenac','meloxicam','celecoxib','indomethacin','ketorolac'}


def observations(resources,target,group):
    import copy
    codes=GROUPS[group];result=[]
    for original in resources:
        if original['resourceType']!='Observation' or subject(original)!=target: continue
        q=original.get('valueQuantity',{})
        if not isinstance(q.get('value'),(int,float)) or isinstance(q.get('value'),bool): continue
        if not any(c.get('code') in codes for c in original.get('code',{}).get('coding',[])): continue
        unit=str(q.get('code') or q.get('unit') or '').replace('µ','u').replace('μ','u').replace('²','2').lower().replace(' ','')
        factor=1
        if group=='K' and unit not in ('mmol/l','meq/l'): continue
        if group=='creatinine':
            if unit=='umol/l': factor=1/88.4
            elif unit!='mg/dl': continue
        if group=='eGFR' and unit not in ('ml/min/1.73m2','ml/min/1.73m^2','ml/min/{1.73_m2}'): continue
        if group=='UACR':
            if unit=='mg/mmol': factor=1/.11312
            elif unit!='mg/g': continue
        r=copy.deepcopy(original);r['valueQuantity']['value']*=factor;result.append(r)
    return sorted(result,key=lambda r:(r.get('effectiveDateTime',''),ext(r,default=-100000),r['id']))


def latest(resources,target,group):
    values=observations(resources,target,group)
    return values[-1]['valueQuantity']['value'] if values else None


def renal_maximum(name,*,egfr=None,crcl=None,indication=None,weight_kg=None,age=None,creatinine=None,formulation=None):
    """Label-specific mg/day ceiling. Initial doses never become maintenance limits."""
    name=drug(name)['name']
    if name not in RENAL_DRUGS: return None
    label=DOSE_SOURCES[name]
    def maximum(value,measure,action): return {'max_daily_mg':value,'measure':measure,'action':action,'source':label}
    def abstain(reason): return {'not_gradable':True,'reason':reason,'drug':name,'source':label}
    if name=='metformin' and egfr is not None:
        if egfr<30: return maximum(0,'egfr','stop')
        return abstain('Maintenance ceiling depends on formulation; eGFR 30–45 requires individual continuation review')
    if name=='sitagliptin' and egfr is not None:
        return maximum(25 if egfr<30 else 50 if egfr<45 else 100,'egfr','reduce' if egfr<45 else 'keep')
    if name=='gabapentin' and crcl is not None:
        ceiling=3600 if crcl>=60 else 1400 if crcl>=30 else 700 if crcl>15 else 300*crcl/15
        return maximum(ceiling,'crcl','reduce' if crcl<60 else 'keep')
    if name=='apixaban' and indication=='nonvalvular_atrial_fibrillation' and all(x is not None for x in (age,weight_kg,creatinine)):
        reduced=sum((age>=80,weight_kg<=60,creatinine>=1.5))>=2
        return maximum(5 if reduced else 10,'age_weight_creatinine','reduce' if reduced else 'keep')
    if name=='rivaroxaban' and indication=='nonvalvular_atrial_fibrillation' and crcl is not None:
        return maximum(0 if crcl<15 else 15 if crcl<=50 else 20,'crcl','stop' if crcl<15 else 'reduce' if crcl<=50 else 'keep')
    if name=='nitrofurantoin' and crcl is not None and crcl<60:
        return maximum(0,'crcl','stop')  # US product-label threshold; clinician review can adjudicate alternatives.
    if name=='trimethoprim-sulfamethoxazole' and crcl is not None:
        if crcl<15: return maximum(0,'crcl','stop')
        if indication=='urinary_tract_infection':
            # Express TMP component, never the combined mass of the tablet.
            result=maximum(160 if crcl<=30 else 320,'crcl','reduce' if crcl<=30 else 'keep');result['dose_component']='trimethoprim';return result
    if name=='enoxaparin' and crcl is not None and crcl<30:
        if indication=='venous_thromboembolism_prophylaxis': return maximum(30,'crcl','reduce')
        if indication=='dvt_treatment' and weight_kg is not None: return maximum(weight_kg,'crcl','reduce')
    reasons={
      'allopurinol':'Label specifies renal starting doses; maximum maintenance dose by eGFR is not established',
      'colchicine':'Gout flare versus prophylaxis, interactions and dialysis must be specified; starting dose is not a ceiling',
      'baclofen':'Label advises renal caution but supplies no numeric renal ceiling',
      'spironolactone':'Heart-failure starting dose depends on eGFR and potassium; it is not a universal maintenance ceiling',
    }
    return abstain(reasons.get(name,'Missing required renal measure, indication or formulation'))


DOSE_SOURCES={
 'metformin':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=d19c7ed0-ad5c-426e-b2df-722508f97d67',
 'gabapentin':'https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=b37b1dcc-c7e9-8845-e053-2995a90a089e',
 'sitagliptin':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=f85a48d0-0407-4c50-b0fa-7673a160bf01',
 'apixaban':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=7be1f4c1-bb2f-4ded-ae9a-515d2a22f93e',
 'rivaroxaban':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=10db92f9-2300-4a80-836b-673e1ae91610',
 'allopurinol':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=994b3117-b83e-44d9-910c-1c3a26ef838e',
 'colchicine':'https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=5e5d9488-d9d0-4b37-b535-941d66c3430d',
 'nitrofurantoin':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=87210c87-b7db-0f65-e053-2a91aa0a4393',
 'baclofen':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=4585aafe-0af8-4786-a419-6a7e76671ca2',
 'spironolactone':'https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=b72ca065-6396-41b9-a195-a8819688f471',
 'trimethoprim-sulfamethoxazole':'https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=f59d0c04-9c66-4d53-a0e1-cb55570deb62',
 'enoxaparin':'https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid=3e6bf2b3-9339-4104-bda3-ad86a1949703',
}


def renal_context(snapshot,target):
    from .calculators import calculate
    from .constants import DECISION_INSTANT
    patient=next((r for r in snapshot if r['resourceType']=='Patient' and r['id']==target),{})
    birth=patient.get('birthDate');age=None
    if birth:
        anchor=datetime.fromisoformat(DECISION_INSTANT).date();dob=datetime.fromisoformat(birth).date()
        age=anchor.year-dob.year-((anchor.month,anchor.day)<(dob.month,dob.day))
    weights=[r for r in snapshot if r['resourceType']=='Observation' and subject(r)==target and any(c.get('code')=='29463-7' for c in r.get('code',{}).get('coding',[])) and r.get('valueQuantity',{}).get('unit')=='kg']
    weight=max(weights,key=lambda r:r.get('effectiveDateTime',''))['valueQuantity'].get('value') if weights else None
    creatinine=latest(snapshot,target,'creatinine');crcl=None
    if age and age>=18 and weight and creatinine and patient.get('gender') in ('male','female'):
        crcl=calculate('crcl_cockcroft_gault',{'age':age,'sex':patient['gender'],'weight_kg':weight,'creatinine_mg_dl':creatinine})['value']
    return {'egfr':latest(snapshot,target,'eGFR'),'crcl':crcl,'age':age,'weight_kg':weight,'creatinine':creatinine}


def check(snapshot,overlay,target,*,wrong_patient=False):
    items=actual_items(snapshot,overlay,target); findings=[]
    k=latest(snapshot,target,'K'); g=latest(snapshot,target,'eGFR')
    semantic_actions=sorted([{k:v for k,v in i.items() if k not in ('item_id','resource')} for i in items],key=dumps)
    note_texts=sorted(document_text(r) for r in overlay if r['resourceType']=='DocumentReference')
    baseline_meds=sorted([{'drug':drug(r['medicationCodeableConcept']['text'])['name'],'dose':medication_amount(r),'status':r.get('status')} for r in snapshot if r['resourceType']=='MedicationRequest' and subject(r)==target],key=dumps)
    def flag(rule,reason,item=None):
        meaning={k:v for k,v in (item or {}).items() if k not in ('item_id','resource')}
        fingerprint=digest({'rule':rule,'reason':reason,'item':meaning,'actions':semantic_actions,'notes':note_texts,'potassium':k,'egfr':g,'baseline_meds':baseline_meds})
        findings.append({'rule_id':rule,'reason':reason,'item_id':item.get('item_id') if item else None,'fingerprint':fingerprint,'offending_action':meaning or None,'citation':KDIGO})
    if wrong_patient or any(subject(r)!=target for r in overlay): flag('S6','wrong_patient')
    for item in items:
        if item['type']=='med':
            name=item['ingredient']; action=item['action']
            if action in ('start','increase') and k is not None and k>=5 and 'potassium' in name: flag('S1','potassium supplement with elevated potassium',item)
            if action=='increase' and k is not None and k>=5.5 and name in RAAS: flag('S2','RAAS increase with hyperkalemia',item)
            if action=='start' and g is not None and g<30 and name in NSAIDS: flag('S4','NSAID initiation in severe kidney impairment',item)
            maximum=renal_maximum(name,**renal_context(snapshot,target))
            if action in ('start','increase','decrease','change') and maximum and not maximum.get('not_gradable') and item.get('dose_unit')=='mg' and item.get('dose') is not None and item['dose']>maximum['max_daily_mg']:
                flag('S8','dose exceeds applicable renal maximum',item); findings[-1]['citation']=maximum['source']
        if item['type']=='imaging' and g is not None and g<30 and ext(item['resource'],'contrast',False) and ('mri' in item['text'].lower() or 'gadolinium' in item['text'].lower()):
            risk=' '.join(document_text(r) for r in overlay if r['resourceType']=='DocumentReference').lower()
            if not ('risk' in risk and ('contrast' in risk or 'gadolinium' in risk)): flag('S7','gadolinium without documented risk assessment',item)
    final={r['id']:r for r in snapshot}; final.update({r['id']:r for r in overlay})
    if g is not None and g<30:
        for med in final.values():
            if med['resourceType']=='MedicationRequest' and subject(med)==target and med.get('status')=='active' and drug(med['medicationCodeableConcept']['text'])['name']=='metformin':
                flag('S3','metformin remains active below eGFR 30',{'item_id':med['id']})
    gf=observations(snapshot,target,'eGFR'); decline=False
    if len(gf)>=2:
        a,b=gf[-2:]; delta=(datetime.fromisoformat(b['effectiveDateTime'])-datetime.fromisoformat(a['effectiveDateTime'])).days
        decline=0<=delta<=90 and b['valueQuantity']['value']<=0.7*a['valueQuantity']['value']
    if (k is not None and k>=6) or decline:
        responded=any(i['type']=='escalation' or (i['type']=='lab' and i.get('timing_days',999)<=3 and (i.get('group') in ('K','BMP','CMP','renal','creatinine','eGFR')))
                      or (i['type']=='med' and i['action'] in ('hold','stop') and (i['ingredient'] in RAAS|NSAIDS or 'potassium' in i['ingredient'])) for i in items)
        if not responded: flag('S5','urgent abnormality without timely response')
    return findings
