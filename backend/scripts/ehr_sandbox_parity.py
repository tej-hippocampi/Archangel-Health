#!/usr/bin/env python3
"""Forty deterministic FHIR search comparisons against an isolated HAPI instance.

Seeds only committed synthetic fixtures. Use a disposable HAPI database.
"""
import argparse,json,sys,urllib.parse,urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asclepius.ehr_sandbox.chart_builder import resources_from_case
from asclepius.ehr_sandbox.visit_compiler import slice_resources,synthetic_resources
from asclepius.ehr_sandbox.sandbox import FhirSandbox,date_value
from asclepius.ehr_sandbox.constants import DECISION_INSTANT
from asclepius.ehr_sandbox.terminology import GROUPS


def fixture():
    case=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/ehr_sandbox/cases/p01.json').read_text())
    resources,_=resources_from_case(case,'parity-source')
    data,target,_=synthetic_resources(slice_resources(resources,0),0,818,0)
    return {'resourceType':'Bundle','type':'collection','identifier':{'value':target['id']},'entry':[{'resource':r} for r in data]}


def queries(bundle):
    patient=next(e['resource'] for e in bundle['entry'] if e['resource']['resourceType']=='Patient');pid=patient['id']
    rows=[]
    for p in ({},{'name':patient['name'][0]['given'][0]},{'family':patient['name'][0]['family']},
              {'given':patient['name'][0]['given'][0]},{'birthdate':patient['birthDate']},{'identifier':patient['identifier'][0]['value']},
              {'gender':patient['gender']},{'gender':'other'}): rows.append(('Patient',p))
    for p in ({},{'code':'2823-3'},{'code':'http://loinc.org|2160-0'},{'code:group':'BMP'},
              {'code:group':'eGFR'},{'category':'laboratory'},{'category':'vital-signs'},
              {'date':'ge2030-01-01'},{'date':'le2031-03-03'},{'date':'lt2031-03-03'},
              {'date':'eq2031-03-03'},{'date':'gt2031-03-03'}): rows.append(('Observation',{'patient':pid,**p}))
    for kind,params in (
      ('MedicationRequest',[{}, {'status':'active'},{'status':'stopped'},{'authoredon':'le2031-03-03'}]),
      ('Condition',[{}, {'clinical-status':'active'},{'code':'N18.4'}]),
      ('DocumentReference',[{}, {'date':'le2031-03-03'},{'date':'ge2030-01-01'},{'date':'gt2031-03-03'}]),
      ('Encounter',[{}, {'date':'le2031-03-03'},{'date':'gt2031-03-03'}]),
      ('ServiceRequest',[{}, {'status':'active'}]),('AllergyIntolerance',[{}, {'patient':'nonexistent'}]),
      ('Appointment',[{}, {'date':'ge2031-03-03'}])):
        rows.extend((kind,{'patient':pid,**p}) for p in params)
    assert len(rows)==40
    return rows


def request(url,body=None):
    req=urllib.request.Request(url,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/fhir+json','Accept':'application/fhir+json'})
    with urllib.request.urlopen(req,timeout=60) as response: return json.load(response)


def run(url):
    bundle=fixture();root=url.rstrip('/');sb=FhirSandbox(bundle,rollout_id='parity',now=DECISION_INSTANT,target_patient_id=bundle['identifier']['value'])
    transaction={'resourceType':'Bundle','type':'transaction','entry':[{'resource':e['resource'],'request':{'method':'PUT','url':e['resource']['resourceType']+'/'+e['resource']['id']}} for e in bundle['entry']]}
    request(root,transaction);failures=[]
    for index,(kind,params) in enumerate(queries(bundle)):
        expected=sb.search(kind,**params,_count=200)
        remote=dict(params)
        if 'name' in remote: remote['name:contains']=remote.pop('name')
        if 'code:group' in remote: remote['code']=','.join(sorted(GROUPS[remote.pop('code:group')]))
        remote['_count']=200
        page=request(root+'/'+kind+'?'+urllib.parse.urlencode(remote,doseq=True));actual=[]
        while True:
            actual.extend(e['resource'] for e in page.get('entry',[]) if e.get('search',{}).get('mode')!='include' and e.get('resource',{}).get('resourceType')==kind)
            nxt=next((l['url'] for l in page.get('link',[]) if l['relation']=='next'),None)
            if not nxt: break
            page=request(nxt)
        # The runtime promises a canonical ordering, independent of server default sort.
        actual.sort(key=lambda r:r['id']);actual.sort(key=date_value,reverse=True)
        want=[e['resource']['id'] for e in expected.get('entry',[])];got=[r['id'] for r in actual]
        if want!=got: failures.append({'query':index+1,'resource':kind,'params':params,'expected':want,'actual':got})
    print(json.dumps({'queries':40,'passed':40-len(failures),'failures':failures},indent=2))
    return int(bool(failures))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);args=parser.parse_args();raise SystemExit(run(args.url))
