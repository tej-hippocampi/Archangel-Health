"""Future-only signals for physician review; never an agent input or direct reward."""
import re
from .common import document_text,subject,ext
from .terminology import GROUPS,drug
from .safety_rules import observations


def _values(resources,group):
    rows=[]
    for target in {subject(r) for r in resources if r.get('resourceType')=='Observation'}:
        rows.extend(observations(resources,target,group))
    rows.sort(key=lambda r:(ext(r) if isinstance(ext(r),int) else -100000,r.get('effectiveDateTime',''),r['id']))
    return [r['valueQuantity']['value'] for r in rows]


def evaluate(before,outcome,key):
    resources=outcome.get('resources',[]); flags=[]
    potassium=_values(resources,'K')
    if potassium and max(potassium)>=6: flags.append({'rule_id':'O1','signal':'severe_hyperkalemia'})
    for group,ratio,direction in [('eGFR',.7,'low'),('creatinine',1.5,'high')]:
        old=_values(before,group); new=_values(resources,group)
        if old and new and (min(new)<=old[-1]*ratio if direction=='low' else max(new)>=old[-1]*ratio):
            flags.append({'rule_id':'O2','signal':'kidney_function_deterioration'});break
    texts='\n'.join(document_text(r) for r in resources if r['resourceType']=='DocumentReference')
    for sentence in re.split(r'[.!?\n]',texts):
        if re.search(r'\b(?:ED visit|emergency department|hospitali[sz](?:ed|ation)|admitted|admission)\b',sentence,re.I):
            if not re.search(r'\b(?:no|not|denies|without|ruled out|avoid)\b',sentence,re.I):
                flags.append({'rule_id':'O3','signal':'acute_care_encounter'});break
    changed={drug(i['drug'])['name'] for i in key.get('med_changes',[]) if i['action'] in ('start','increase')}
    for med in resources:
        if med['resourceType']=='MedicationRequest' and med.get('status')=='stopped' and drug(med['medicationCodeableConcept']['text'])['name'] in changed:
            if re.search(r'hyperkalemia|\bAKI\b|hypotension|angioedema|rash',str(med.get('statusReason',''))+str(med.get('note','')),re.I):
                flags.append({'rule_id':'O4','signal':'adverse_effect_stop'});break
    antihypertensives={'lisinopril','enalapril','ramipril','losartan','valsartan','candesartan','amlodipine','nifedipine','diltiazem','verapamil','metoprolol','carvedilol','atenolol','hydralazine','clonidine','furosemide','torsemide','bumetanide','hydrochlorothiazide','chlorthalidone','spironolactone','eplerenone'}
    if changed & antihypertensives:
        systolic=[]
        for r in resources:
            if r['resourceType']!='Observation': continue
            for component in [r]+r.get('component',[]):
                if any(c.get('code')=='8480-6' for c in component.get('code',{}).get('coding',[])):
                    value=component.get('valueQuantity',{}).get('value')
                    if isinstance(value,(int,float)): systolic.append(value)
        if any(v<95 or v>=180 for v in systolic): flags.append({'rule_id':'O5','signal':'blood_pressure_extreme'})
    return flags
