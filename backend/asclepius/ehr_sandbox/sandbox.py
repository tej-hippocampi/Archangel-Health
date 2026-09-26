"""Deterministic FHIR search and copy-on-write persistence; no source-store access."""
from __future__ import annotations
import base64
import copy
import json
import math
import re
from datetime import datetime
from functools import lru_cache
from collections import defaultdict

from .common import digest, document_text, outcome, resource_ref, subject, validate
from .terminology import GROUPS,frequency_per_day

PARAMS = {
    'Patient': {'name','family','given','birthdate','identifier','gender'},
    'Observation': {'patient','code','code:group','category','date'},
    'Condition': {'patient','clinical-status','code'},
    'MedicationRequest': {'patient','status','authoredon'},
    'DocumentReference': {'patient','type','date'},
    'Encounter': {'patient','date'},
    'ServiceRequest': {'patient','status','category','authored'},
    'Appointment': {'patient','date'},
    'AllergyIntolerance': {'patient'},
}
WRITABLE = {'MedicationRequest','ServiceRequest','Appointment','Condition','DocumentReference','Flag','CommunicationRequest'}
TRANSITIONS = {'active': {'active','stopped','on-hold','cancelled'}, 'on-hold': {'on-hold','active','stopped','cancelled'}}


def date_value(resource):
    return next((resource[k] for k in ('effectiveDateTime','authoredOn','date','recordedDate','start') if resource.get(k)),
                resource.get('period',{}).get('start',''))


def _codes(value):
    if isinstance(value,list): return [pair for item in value for pair in _codes(item)]
    return [(c.get('system',''),c.get('code','')) for c in (value or {}).get('coding',[])]


def _code_match(value,query):
    return any((system+'|'+code==query if '|' in query else code==query) for system,code in _codes(value))


def _date_match(value,query,*,period=False,end=None):
    query=str(query); prefix=query[:2] if query[:2] in ('eq','ge','gt','le','lt') else 'eq'
    expected=query[2:] if query[:2]==prefix else query
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',expected): raise ValueError('date filters require YYYY-MM-DD')
    datetime.strptime(expected,'%Y-%m-%d')
    if not value: return False
    actual=value[:10]
    if period:
        upper=end[:10] if end else '9999-12-31'
        return {'eq':actual==expected and upper==expected,'ge':upper>=expected,'gt':upper>expected,'le':actual<=expected,'lt':actual<expected}[prefix]
    return {'eq':actual==expected,'ge':actual>=expected,'gt':actual>expected,'le':actual<=expected,'lt':actual<expected}[prefix]


class _FrozenDict(dict):
    def _deny(self,*a,**k): raise TypeError('immutable task snapshot')
    __setitem__=__delitem__=clear=pop=popitem=setdefault=update=__ior__=_deny
    def __deepcopy__(self,memo): return {k:copy.deepcopy(v,memo) for k,v in self.items()}

class _FrozenList(list):
    def _deny(self,*a,**k): raise TypeError('immutable task snapshot')
    __setitem__=__delitem__=append=clear=extend=insert=pop=remove=reverse=sort=__iadd__=__imul__=_deny
    def __deepcopy__(self,memo): return [copy.deepcopy(v,memo) for v in self]

def _freeze(value):
    if isinstance(value,dict): return _FrozenDict((k,_freeze(v)) for k,v in value.items())
    if isinstance(value,list): return _FrozenList(_freeze(v) for v in value)
    return value


class SnapshotIndex:
    """Private immutable-by-convention index; all API returns are deep copies."""
    def __init__(self,bundle):
        resources=[_freeze(e['resource']) for e in bundle.get('entry',[])]
        self.resources=_FrozenDict((resource_ref(r),r) for r in resources)
        if len(self.resources)!=len(resources): raise ValueError('duplicate resource identifiers')
        self.by_type=defaultdict(dict);self.by_patient=defaultdict(dict);self.by_code=defaultdict(set)
        for ref,r in self.resources.items():
            self.by_type[r['resourceType']][ref]=r
            self.by_patient[(r['resourceType'],subject(r))][ref]=r
            if r['resourceType']=='Observation':
                for system,code in _codes(r.get('code')):
                    self.by_code[code].add(ref);self.by_code[system+'|'+code].add(ref)
        self.dates=frozenset(str(v)[:10] for r in resources for k,v in r.items()
                            if k in ('birthDate','effectiveDateTime','authoredOn','date','recordedDate','start') and isinstance(v,str))
        self.target=bundle['identifier']['value']


@lru_cache(maxsize=32)
def cached_snapshot(task_id,snapshot_json):
    return SnapshotIndex(json.loads(snapshot_json))


class FhirSandbox:
    def __init__(self,snapshot,*,rollout_id,now,target_patient_id):
        if now != '2031-03-03T09:00:00-08:00': raise ValueError('unsupported decision instant')
        self._index=snapshot if isinstance(snapshot,SnapshotIndex) else SnapshotIndex(snapshot)
        self._snapshot=self._index.resources
        if 'Patient/'+target_patient_id not in self._snapshot: raise ValueError('target patient missing')
        self.overlay={}; self.rollout_id=rollout_id; self.now=now; self.target_patient_id=target_patient_id
        self.access_log=[]; self.write_log=[]; self.rejected_writes=[]; self.wrong_patient=False
        self.step=0; self.tool=''; self._sequence=0
        self._synthetic_dates=self._index.dates | {now[:10]}

    def resources(self):
        return copy.deepcopy(list((self._snapshot | self.overlay).values()))

    def _scrub(self,resource):
        # The import is replaced by the same scanner in the standalone package.
        from asclepius.validation import residual_identifiers
        result=copy.deepcopy(resource)
        texts=[document_text(result)] if result['resourceType']=='DocumentReference' else []
        def clinical_texts(node):
            if isinstance(node,dict):
                for key,value in node.items():
                    if key in ('text','display','description','contentString','div') and isinstance(value,str): texts.append(value)
                    else: clinical_texts(value)
            elif isinstance(node,list):
                for value in node: clinical_texts(value)
        clinical_texts(result)
        trusted_dates=set(self._synthetic_dates)
        for item in self.overlay.values():
            for field in ('occurrenceDateTime','authoredOn','recordedDate','date','start','end'):
                value=item.get(field)
                if isinstance(value,str) and re.match(r'^\d{4}-\d{2}-\d{2}',value): trusted_dates.add(value[:10])
        identities=set();names=set()
        for patient in self._index.by_type.get('Patient',{}).values():
            names.update(n['text'] for n in patient.get('name',[]) if n.get('text'))
            identities.update(i['value'] for i in patient.get('identifier',[]) if i.get('value'))
        for day in tuple(trusted_dates):
            year,month,date=day.split('-')
            trusted_dates.update((f'{month}/{date}/{year}',f'{int(month)}/{int(date)}/{year}'))
        for text in texts:
            # Mask only a complete synthetic Patient.name or complete labeled
            # patient-name value. Never mask a trusted substring inside a name.
            if text in names: text='[synthetic identity]'
            def patient_label(match):
                value=match.group(2)
                parts=re.split(r'(?:[,;]?\s+|\s*\()(?:MRN|DOB)\s*[:#]',value,maxsplit=1,flags=re.I)
                label_value=parts[0].strip().rstrip('.')
                if label_value in names:
                    return match.group(1)+value.replace(label_value,'[synthetic identity]',1)
                return match.group(0)
            text=re.sub(r'(?im)(\b(?:patient(?:\s+name)?|name)\s*:\s*)([^\n]+)',patient_label,text)
            for value in sorted(identities,key=len,reverse=True):
                text=re.sub(r'(?<!\w)'+re.escape(value)+r'(?!\w)','[synthetic identity]',text)
            for day in sorted(trusted_dates,key=len,reverse=True): text=text.replace(day,'[synthetic date]')
            if residual_identifiers(text): raise ValueError('residual_identifier_blocked')
        return result

    def _read_log(self,params,resources):
        self.access_log.append({'step':self.step,'tool':self.tool,'params':copy.deepcopy(params),
                                'returned_ids':[resource_ref(r) for r in resources]})

    def read(self,kind,resource_id,*,metadata=False):
        item=self.overlay.get(kind+'/'+resource_id,self._snapshot.get(kind+'/'+resource_id))
        if not item: return outcome('resource not found','not-found')
        try: result=self._scrub(item)
        except (ValueError,UnicodeError): return outcome('residual_identifier_blocked','security')
        if metadata and kind=='DocumentReference': result.pop('content',None)
        self._read_log({'id':resource_id},[item])
        return result

    def search(self,kind,**params):
        if kind not in PARAMS: return outcome('unsupported resource type','not-supported')
        unknown=set(params)-PARAMS[kind]-{'_sort','_count','_page'}
        if unknown: return outcome('unsupported parameters: '+', '.join(sorted(unknown)),'not-supported')
        try:
            count=params.get('_count',50)
            if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=200: raise ValueError('_count must be 1 through 200')
            if params.get('_sort','-date') not in ('date','-date'): raise ValueError('unsupported sort')
            query_hash=digest([kind,{k:v for k,v in params.items() if k!='_page'}])[:20]
            start=0
            if params.get('_page'):
                token=json.loads(base64.urlsafe_b64decode(params['_page']).decode())
                if token['q']!=query_hash or not isinstance(token['n'],int) or token['n']<0: raise ValueError('invalid page token')
                start=token['n']
            records=[]
            patient=params.get('patient')
            candidates=self._index.by_patient.get((kind,str(patient).removeprefix('Patient/')), {}) if patient is not None and not isinstance(patient,list) else self._index.by_type.get(kind,{})
            if kind=='Observation' and isinstance(params.get('code'),str):
                refs=self._index.by_code.get(params['code'],set());candidates={ref:r for ref,r in candidates.items() if ref in refs}
            candidates={ref:r for ref,r in candidates.items() if ref not in self.overlay}
            candidates.update({ref:r for ref,r in self.overlay.items() if r['resourceType']==kind})
            for item in candidates.values():
                matched=True
                for key,value in params.items():
                    if key.startswith('_'): continue
                    values=value if isinstance(value,list) else [value]
                    for val in values:
                        if key=='patient': ok=subject(item)==str(val).removeprefix('Patient/')
                        elif key in ('name','family','given'):
                            parts=[n.get('text','')+' '+n.get('family','')+' '+' '.join(n.get('given',[])) if key=='name'
                                   else ' '.join(n.get('given',[])) if key=='given' else n.get('family','') for n in item.get('name',[])]
                            ok=any(str(val).casefold() in p.casefold() for p in parts)
                        elif key=='identifier': ok=any(str(val)==str(n.get('value')) for n in item.get('identifier',[]))
                        elif key=='gender': ok=item.get('gender')==val
                        elif key in ('birthdate','date','authored','authoredon'):
                            ok=_date_match(item.get('birthDate','') if key=='birthdate' else date_value(item),val,period=kind=='Encounter' and key=='date',end=item.get('period',{}).get('end'))
                        elif key=='status': ok=item.get('status')==val
                        elif key=='clinical-status': ok=_code_match(item.get('clinicalStatus'),val)
                        elif key=='code': ok=_code_match(item.get('code'),val)
                        elif key=='category': ok=_code_match(item.get('category'),val)
                        elif key=='type': ok=str(val).casefold() in item.get('type',{}).get('text','').casefold()
                        elif key=='code:group':
                            group=next((codes for group,codes in GROUPS.items() if group.lower()==str(val).lower()),None)
                            if group is None: raise ValueError('unknown LOINC group')
                            ok=any(code in group for _,code in _codes(item.get('code')))
                        else: ok=False
                        matched=matched and ok
                if matched: records.append(item)
            records.sort(key=lambda r:r['id'])
            records.sort(key=date_value,reverse=params.get('_sort','-date')=='-date')
            page=records[start:start+count]; clean=[self._scrub(r) for r in page]
            if kind=='DocumentReference':
                for r in clean: r.pop('content',None)
            result={'resourceType':'Bundle','type':'searchset','total':len(records),'entry':[{'resource':r} for r in clean]}
            if start+count<len(records):
                token=base64.urlsafe_b64encode(json.dumps({'q':query_hash,'n':start+count},separators=(',',':')).encode()).decode()
                result['link']=[{'relation':'next','url':'?_page='+token}]
            self._read_log(params,page)
            return result
        except (ValueError,TypeError,KeyError,UnicodeError) as exc:
            return outcome('residual_identifier_blocked' if str(exc)=='residual_identifier_blocked' else 'invalid search: '+str(exc)[:120])

    def _prepare(self,resource,*,resource_id=None):
        item=copy.deepcopy(resource); kind=item.get('resourceType'); patient=subject(item)
        if kind not in WRITABLE: raise ValueError('resource type is not writable')
        if not patient or 'Patient/'+patient not in self._snapshot: raise ValueError('subject must reference an existing patient')
        if patient!=self.target_patient_id:
            self.wrong_patient=True
            raise ValueError('wrong_patient: write rejected')
        old=None
        if resource_id:
            old=self.overlay.get(kind+'/'+resource_id,self._snapshot.get(kind+'/'+resource_id))
            if not old: raise ValueError('update target not found')
            if subject(old)!=patient: raise ValueError('subject cannot change')
            if item.get('id') not in (None,resource_id): raise ValueError('id cannot change')
            if kind=='MedicationRequest' and any(item.get(field)!=old.get(field) for field in ('medicationCodeableConcept','dosageInstruction')):
                raise ValueError('regimen changes require a new linked prescription')
            if kind=='MedicationRequest' and item.get('status') not in TRANSITIONS.get(old.get('status'),{old.get('status')}):
                raise ValueError('illegal medication status transition')
            item['id']=resource_id
        else:
            item['id']='w-'+digest([self.rollout_id,self._sequence])[:16]
        if kind in ('MedicationRequest','ServiceRequest','CommunicationRequest'): item['authoredOn']=self.now
        if kind=='Condition': item['recordedDate']=self.now
        if kind=='DocumentReference': item['date']=self.now
        if kind=='MedicationRequest':
            med=item.get('medicationCodeableConcept',{})
            if not med.get('text','').strip() and not med.get('coding'): raise ValueError('medication is required')
            instructions=item.get('dosageInstruction')
            if not isinstance(instructions,list) or not instructions or not isinstance(instructions[0],dict): raise ValueError('medication dosage is required')
            dosage=instructions[0]
            rates=dosage.get('doseAndRate',[])
            quantities=[r.get('doseQuantity',{}).get('value') for r in rates]
            if quantities:
                if any(isinstance(q,bool) or not isinstance(q,(int,float)) or not math.isfinite(q) or q<=0 for q in quantities):
                    raise ValueError('medication dose must be positive and finite')
            else:
                parsed=re.search(r'(?<![\w.])(-?\d+(?:\.\d+)?)\s*(?:mg|mcg|g|mEq|mL|units?)\b',dosage.get('text',''),re.I)
                if not parsed or float(parsed.group(1))<=0: raise ValueError('positive parseable medication dosage is required')
            frequency=dosage.get('timing',{}).get('code',{}).get('text') or dosage.get('text','')
            if item.get('status')=='active' and frequency_per_day(frequency) is None: raise ValueError('medication frequency must be explicit and parseable')
        # References cannot manufacture unseen resources or cross patient boundaries.
        def references(node):
            if isinstance(node,dict):
                if 'reference' in node:
                    ref=node['reference']; target=self.overlay.get(ref,self._snapshot.get(ref))
                    if not target: raise ValueError('reference target does not exist')
                    if subject(target)!=patient: raise ValueError('reference belongs to another patient')
                for child in node.values(): references(child)
            elif isinstance(node,list):
                for child in node: references(child)
        references(item)
        try: validate(item)
        except Exception: raise ValueError('resource does not conform to FHIR R4B') from None
        self._scrub(item)
        return item

    def write_many(self,operations):
        """Atomic multi-resource changes (dose replacement and urgent escalation)."""
        checkpoint=copy.deepcopy(self.overlay); sequence=self._sequence; results=[]
        try:
            for resource,resource_id in operations:
                item=self._prepare(resource,resource_id=resource_id)
                self.overlay[resource_ref(item)]=item; self._sequence+=1; results.append(item)
        except Exception as exc:
            self.overlay=checkpoint; self._sequence=sequence
            entry={'step':self.step,'tool':self.tool,'resource_type':resource.get('resourceType'),
                   'id':resource_id,'accepted':False,'diagnostics':str(exc) if isinstance(exc,ValueError) else 'invalid resource shape'}
            self.rejected_writes.append(entry); self.write_log.append(entry)
            return outcome(entry['diagnostics'])
        for item in results:
            self.write_log.append({'step':self.step,'tool':self.tool,'resource_type':item['resourceType'],'id':item['id'],'accepted':True})
        return copy.deepcopy(results)

    def create(self,resource):
        result=self.write_many([(resource,None)])
        return result[0] if isinstance(result,list) else result

    def update(self,resource_id,resource):
        result=self.write_many([(resource,resource_id)])
        return result[0] if isinstance(result,list) else result
