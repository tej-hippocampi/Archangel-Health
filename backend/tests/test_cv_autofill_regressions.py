"""Behavioral regressions beyond the generated 100-document development corpus."""
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from asclepius import credentialing as cv

@pytest.mark.parametrize('name',['José Álvarez','Zoë Laurent','Jean-Luc Martin',"Rachel O’Neill"])
def test_unicode_and_titled_names(name):
    assert cv._parse_cv_text(f'Dr. {name}, MD\nCurriculum Vitae')['full_name'] == name

@pytest.mark.parametrize('institution',['Cleveland Clinic','Johns Hopkins Hospital','Baylor College of Medicine'])
def test_undelimited_institution_names(institution):
    result=cv._parse_cv_text(f'Jane Smith, MD\n{institution} 2015-2017\nFellow, Nephrology')
    assert result['training'][0]['institution']==institution
    assert result['training'][0]['specialty']=='Nephrology'
    assert cv._extract_employer([f'{institution} 2020-present','Attending physician'])==institution

def test_training_without_dates_still_fills_institution():
    result=cv._parse_cv_text('Jane Smith, MD\nFellowship, Nephrology — Cleveland Clinic')
    assert result['training'][0]['institution']=='Cleveland Clinic'
    assert result['training'][0]['end_year'] is None

def test_heading_not_applicant():
    text='\n'.join(['Curriculum Vitae']+['Department contact information']*8+['Board Certifications','Jane Smith, MD'])
    assert cv._parse_cv_text(text)['full_name']=='Jane Smith'

@pytest.mark.parametrize('claim',['Not board certified in Nephrology (ABIM)', 'Board eligible in Nephrology, ABIM exam pending', 'Board certification in Nephrology (ABIM) scheduled'])
def test_negative_board_claims(claim):
    assert cv._parse_cv_text('Jane Smith, MD\n'+claim)['board_certifications_structured']==[]

def test_reference_degree_and_phone_are_not_applicant():
    p=cv._parse_cv_text('Jane Smith, MBBS\nPhone: 555-010-0000\nReferences\nJohn Other, MD, PhD\nMobile: 555-010-2222')
    assert p['degrees']==['MBBS']
    assert p['mobile_phone'] is None

def test_explicit_contact_focus_and_years():
    p=cv._parse_cv_text('Jane Smith, MD\nMobile: +1 555 010 4444\nPractice city: Boston\nClinical focus: CKD and dialysis\n7 years of clinical practice')
    assert p['mobile_phone']=='+1 555 010 4444'
    assert p['practice_city']=='Boston'
    assert p['clinical_focus']=='CKD and dialysis'
    assert p['years_in_active_practice']==7

def test_elapsed_training_years_do_not_claim_active_practice():
    p=cv._parse_cv_text('Jane Smith, MD\nResidency, Internal Medicine — Example Hospital, 2000-2003\nCareer break 2003-present')
    assert p['years_in_active_practice'] is None

def test_not_active_license_and_publications_not_employer():
    p=cv._parse_cv_text('Jane Smith, MD\nCalifornia License: A123456 - Not active\nSelected Publications 2020-present')
    assert p['licenses'][0]['current']==''
    assert p['employer']==''

def test_mixed_pdf_ocr_is_per_page(monkeypatch):
    from PyPDF2 import PdfReader, PdfWriter
    source=Path(__file__).parent/'cv_corpus/pdfs/cv-000.pdf'
    writer=PdfWriter(); writer.add_page(PdfReader(str(source)).pages[0]); writer.add_blank_page(width=612,height=792)
    out=io.BytesIO(); writer.write(out)
    calls=[]
    def ocr(data,max_pages):
        calls.append(len(PdfReader(io.BytesIO(data)).pages))
        return 'Additional scanned credential text'
    monkeypatch.setattr(cv,'_ocr_pdf_pages',ocr)
    text=cv.extract_cv_text(out.getvalue(),'application/pdf')
    assert 'Avery Morgan' in text and 'Additional scanned credential text' in text
    assert calls==[1]

def test_pdf_page_limit_precedes_ocr(monkeypatch):
    from PyPDF2 import PdfWriter
    writer=PdfWriter()
    for _ in range(31): writer.add_blank_page(width=612,height=792)
    out=io.BytesIO(); writer.write(out)
    monkeypatch.setattr(cv,'_ocr_pdf_pages',lambda *a,**k: pytest.fail('must reject before OCR'))
    with pytest.raises(ValueError,match='page_limit'): cv.extract_cv_text(out.getvalue(),'application/pdf')

def test_100_pdf_to_actual_typescript_fields():
    node=os.getenv('NODE_BINARY') or shutil.which('node')
    if not node or int(subprocess.check_output([node,'-p','process.versions.node.split(".")[0]'],text=True))<22:
        if os.getenv('CV_REQUIRE_CORPUS'): pytest.fail('Node 24 is required in the CV validation job')
        pytest.skip('Node 24 required; mandatory in cv-autofill workflow')
    os.environ['NODE_BINARY']=node
    from tests.cv_corpus.evaluate import evaluate
    report=evaluate()
    assert report['documents']==100 and report['field_checks']==1300
    assert report['failures']==[]

@pytest.mark.parametrize('filename',['scan.pdf','mixed-scan.pdf'])
def test_real_ocr_fixtures(filename):
    from asclepius.dicom_deid import ocr_available
    if not ocr_available() or not shutil.which('pdftoppm'):
        if os.getenv('CV_REQUIRE_OCR'): pytest.fail('OCR binaries required by CV validation job')
        pytest.skip('Tesseract/Poppler unavailable locally; real OCR mandatory in CV workflow')
    raw=(Path(__file__).parent/'cv_corpus'/filename).read_bytes()
    p=cv._parse_cv_text(cv.extract_cv_text(raw,'application/pdf'))
    assert p['full_name']=='Avery Morgan'
    assert p['licenses'][0]['number']=='A900000'
    assert p['training'][0]['institution']=='Example A University Hospital'
