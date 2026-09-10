"""All 100 synthetic PDFs through real upload/storage/status and TS autofill."""
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from tests.test_onboarding_v2 import client, _stub_email, _seed_verified  # actual HTTP fixtures


def test_all_100_uploaded_pdfs_fill_expected_fields(client):
    node=os.getenv('NODE_BINARY') or shutil.which('node')
    if not node or int(subprocess.check_output([node,'-p','process.versions.node.split(".")[0]'],text=True))<22:
        if os.getenv('CV_REQUIRE_CORPUS'): pytest.fail('Node 24 required')
        pytest.skip('Node 24 required; enforced in CV workflow')
    root=Path(__file__).parent/'cv_corpus'
    cases=json.loads((root/'manifest.json').read_text())
    token,_,_=_seed_verified(client)
    results=[]
    for case in cases:
        filename=case['id']+'.pdf'
        response=client.post('/api/onboarding/asclepius/cv',data={'token':token},files={'file':(filename,(root/'pdfs'/filename).read_bytes(),'application/pdf')})
        assert response.status_code==200,response.text
        status=client.get('/api/onboarding/asclepius/cv/status',params={'token':token}).json()
        assert status['finished'] and status['ok'],(filename,status)
        assert status['attempt_id']==response.json()['attempt_id']
        assert status['filename']==filename
        assert status['parser_version']=='cv-parse-2'
        results.append(status['parsed'])
    forms=json.loads(subprocess.check_output([node,str(root/'map.cjs')],input=json.dumps(results),text=True))
    failures=[(case['id'],field,expected,form.get(field)) for case,form in zip(cases,forms)
              for field,expected in case['expected'].items() if form.get(field)!=expected]
    assert not failures,failures
