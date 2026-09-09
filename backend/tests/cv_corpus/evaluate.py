"""Exercise actual PDF extraction -> actual onboarding mapper -> ground truth."""
import json, os, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parents[1]))
from asclepius.credentialing import extract_cv_text, _parse_cv_text

def evaluate():
    cases = json.loads((ROOT / 'manifest.json').read_text())
    start = time.monotonic()
    parsed = [dict(_parse_cv_text(extract_cv_text((ROOT/'pdfs'/f'{c["id"]}.pdf').read_bytes(), 'application/pdf')), ok=True) for c in cases]
    result = subprocess.run([os.environ.get('NODE_BINARY','node'),str(ROOT/'map.cjs')],input=json.dumps(parsed),text=True,capture_output=True,check=True)
    forms = json.loads(result.stdout)
    failures, total = [], 0
    for case, form in zip(cases, forms):
        for field, expected in case['expected'].items():
            total += 1
            if form.get(field) != expected:
                failures.append({'id':case['id'],'field':field,'expected':expected,'actual':form.get(field)})
    return {'documents':len(cases),'field_checks':total,'passed':total-len(failures),
            'documents_passed':len(cases)-len({f['id'] for f in failures}),
            'elapsed_seconds':round(time.monotonic()-start,2),'failures':failures}

if __name__ == '__main__':
    report=evaluate()
    if len(sys.argv)>1: Path(sys.argv[1]).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='failures'}))
