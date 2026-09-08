"""Local-only adversarial fixtures; imports existing parser, no stores/network."""
from pathlib import Path
import sys,io,json,hashlib,importlib.util,os
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parent
REPO=Path(os.environ.get('ARCHANGEL_REPO', str(Path.cwd()))).resolve()
sys.path.insert(0,str(REPO/'backend'))
from asclepius import credentialing as c
OUT=Path(__file__).parent
cases=[]
def case(name,text,checks):
 parsed=c._parse_cv_text(text)
 got={k:parsed.get(k) for k in checks}
 cases.append(dict(name=name,input=text,expected=checks,actual=got,passed=got==checks,parsed=parsed))
base=(ROOT/'Mock_Physician_CV_Mike_Blum.txt').read_text()
case('baseline',base,{'full_name':'Mike Blum','specialty':'nephrology','years_in_practice':10,'employer':'Cedar Example Medical Center'})
case('accented_name','José Muñoz, MD\nNephrology\n12 years of clinical practice',{'full_name':'José Muñoz'})
case('honorific','Dr. Mike Blum, MD\nNephrology\n10 years of clinical practice',{'full_name':'Mike Blum'})
case('all_caps_name','MIKE BLUM, MD\nNephrology\n10 years of clinical practice',{'full_name':'MIKE BLUM'})
case('name_after_nine_heading_lines','\n'.join(['CURRICULUM VITAE']*9+['Mike Blum, MD','Nephrology']),{'full_name':'Mike Blum'})
case('no_degree_from_reference','Mike Blum\nNephrology\nReferences\nSarah Example, MD\nExample Hospital',{'degrees':[]})
case('no_board_from_negation','Mike Blum, MD\nNot board certified in Nephrology\n10 years of clinical practice',{'board_certifications_structured':[]})
case('no_board_from_in_progress','Mike Blum, MD\nBoard certification in Nephrology (ABIM) pending examination',{'board_certifications_structured':[]})
case('board_specialty_priority','Mike Blum, MD\nPrimary specialty: Nephrology\nBoard Certified in Internal Medicine (ABIM)\nBoard Certified in Nephrology (ABIM)',{'specialty':'nephrology'})
case('single_line_training','Mike Blum, MD\nNephrology Fellowship, Cedar Example Medical Center, 2014-2016',{'training':[{'kind':'fellowship','institution':'Cedar Example Medical Center','start_year':'2014','end_year':'2016'}]})
case('training_month_ranges','Mike Blum, MD\nCedar Example Medical Center    Jul 2014 - Jun 2016\nFellow, Nephrology',{'training':[{'kind':'fellowship','institution':'Cedar Example Medical Center','start_year':'2014','end_year':'2016'}]})
case('ambiguous_years_since_training','Mike Blum, MD\nCedar Example Medical Center    2014 - 2016\nFellow, Nephrology',{'years_in_practice':None})
case('single_line_inferred_active_years','Mike Blum, MD\nNephrology Fellowship, Cedar Example Medical Center, 2014-2016\nCareer break 2016-2025',{'years_in_practice':None})
case('license_no_hash','Mike Blum, MD\nCalifornia Medical License: A123456 - Active',{'licenses':[{'state':'CA','number':'A123456','current':'yes'}]})
case('license_not_active','Mike Blum, MD\nCalifornia Medical License #TEST-900001 - Not active',{'licenses':[{'state':'CA','number':'TEST-900001','current':''}]})
case('license_split_lines','Mike Blum, MD\nCalifornia Medical License\n#TEST-900001 - Active',{'licenses':[{'state':'CA','number':'TEST-900001','current':'yes'}]})
case('employer_lowercase_present','Mike Blum, MD\nCedar Example Medical Center    2016 - present\nAttending Physician, Nephrology',{'employer':'Cedar Example Medical Center'})
case('not_employer_publication_header','Mike Blum, MD\nSelected Publications    2020 - Present\nArticles about Nephrology',{'employer':''})
case('explicit_mobile','Mike Blum, MD\nMobile: +1 (202) 555-0147\nOffice: +1 (202) 555-0180',{'phone':'+1 (202) 555-0147'})
case('explicit_languages','Mike Blum, MD\nLanguages: English, Spanish\nNephrology',{'languages':['English','Spanish']})
case('international_primary_qualification','Asha Example, MBBS\nPrimary medical qualification: MBBS\nPostgraduate MD, Internal Medicine\nGMC registration: 7654321\nCountry of licensure: United Kingdom',{'primary_qualification':'MBBS','registration_number':'7654321','country_of_licensure':'GB'})
case('unlabelled_phone_not_npi','Mike Blum, MD\nPhone: 9999999995\nNephrology',{'npi':None})
case('bad_checksum_not_npi','Mike Blum, MD\nNPI: 9999999994\nNephrology',{'npi':None})
case('single_board_acronym_not_certification','Mike Blum, MD\nInterested in ABIM\nNephrology',{'board_certifications_structured':[]})
case('future_fellowship','Mike Blum, MD\nCedar Example Medical Center    2026 - 2028\nFellow, Nephrology',{'training':[{'kind':'fellowship','institution':'Cedar Example Medical Center','start_year':'2026','end_year':'2028'}],'years_in_practice':None})
case('specialty_from_publication_not_identity','Mike Blum, MD\nPrimary specialty: Cardiology\nPublications\nChen et al. Nephrology evaluation. 2024.',{'specialty':'cardiology'})
case('multiline_board','Mike Blum, MD\nBoard Certified in Nephrology\nAmerican Board of Internal Medicine',{'board_certifications_structured':[{'board':'ABIM','specialty':'Nephrology','subspecialty':'','active':None}]})
# Existing corpus expectation checks (no conftest or application boot).
spec=importlib.util.spec_from_file_location('cv_fixtures',REPO/'backend/tests/fixtures/cvs/__init__.py')
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
existing=[]
for fx in f.CV_FIXTURES:
 p=c._parse_cv_text(fx['text']);errors=[]
 for key,expected in fx['expect'].items():
  if key=='board_labels_must_not_contain':
   if any(x in [b['specialty'] for b in p['board_certifications_structured']] for x in expected):errors.append(key)
  elif key in ('training','licenses','board_certifications_structured'):
   actual=p[key]
   if len(actual)!=len(expected) or any(any(g.get(k)!=v for k,v in w.items()) for g,w in zip(actual,expected)): errors.append(key)
  elif p.get(key)!=expected:errors.append(key)
 existing.append({'name':fx['name'],'passed':not errors,'errors':errors})
# Real PDF text layers, columns, and mixed text/raster content.
pdfs=[]
def pdf_probe(name,draw,checks):
 buf=io.BytesIO();cv=canvas.Canvas(buf,pagesize=(612,792));draw(cv);cv.save();data=buf.getvalue()
 (OUT/(name+'.pdf')).write_bytes(data)
 try:
  text=c.extract_cv_text(data,'application/pdf');parsed=c._parse_cv_text(text)
  actual={k:parsed.get(k) for k in checks}
  pdfs.append({'name':name,'expected':checks,'actual':actual,'passed':actual==checks,'text_chars':len(text),'extracted_text':text})
 except Exception as exc:pdfs.append({'name':name,'passed':False,'error':type(exc).__name__})
def lines(cv,items,x=45,y=745,size=10):
 cv.setFont('Helvetica',size)
 for item in items:cv.drawString(x,y,item);y-=16
pdf_probe('single_column',lambda cv:lines(cv,base.splitlines()),{'full_name':'Mike Blum','specialty':'nephrology','npi':'9999999995'})
pdf_probe('two_columns',lambda cv:(lines(cv,['Mike Blum, MD','Nephrology','10 years of clinical practice','California Medical License #TEST-900001 - Active'],size=8),lines(cv,['Cedar Example Medical Center    2014 - 2016','Fellow, Nephrology','Harbor Example University Hospital    2011 - 2014','Resident, Internal Medicine'],x=325,y=680,size=7)),{'full_name':'Mike Blum','specialty':'nephrology','training':[{'kind':'fellowship','institution':'Cedar Example Medical Center','start_year':'2014','end_year':'2016'},{'kind':'residency','institution':'Harbor Example University Hospital','start_year':'2011','end_year':'2014'}]})
img=Image.new('RGB',(1500,500),'white');d=ImageDraw.Draw(img)
try:
 font=ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',35)
except OSError:
 font=ImageFont.truetype('DejaVuSans.ttf',35)
d.multiline_text((30,25),'Mike Blum, MD\nNephrology\nNPI: 9999999995\n10 years of clinical practice',fill='black',font=font,spacing=20)
pdf_probe('mixed_text_and_scan',lambda cv:(lines(cv,['CURRICULUM VITAE - SYNTHETIC TEST COPY FOR EXTRACTION QA']),cv.drawImage(ImageReader(img),40,420,width=530,height=177)),{'full_name':'Mike Blum','npi':'9999999995'})
pdf_probe('image_only',lambda cv:cv.drawImage(ImageReader(img),40,420,width=530,height=177),{'full_name':'Mike Blum','npi':'9999999995'})
pdf_probe('blank',lambda cv:cv.showPage(),{'full_name':None,'npi':None})
def multi(cv):
 lines(cv,['Mike Blum, MD','Nephrology']);cv.showPage();lines(cv,['Board Certified in Nephrology (ABIM)','NPI: 9999999995'])
pdf_probe('multipage',multi,{'full_name':'Mike Blum','npi':'9999999995','board_certifications':['ABIM Nephrology']})
result={'scope':'Existing parser executed locally; targeted diagnostic corpus, not population accuracy. New expectations are proposed acceptance behavior, including missing capabilities. No uploads or registry calls.','text_cases':cases,'existing_fixture_checks':existing,'pdf_cases':pdfs}
(OUT/'probe_results.json').write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
for kind,rows in [('existing',existing),('text',cases),('pdf',pdfs)]:
 print(kind,len(rows),'pass',sum(x['passed'] for x in rows),'fail',sum(not x['passed'] for x in rows))
 for x in rows:
  if not x['passed']:print(' GAP',x['name'],json.dumps(x.get('actual',x.get('error')),ensure_ascii=False))
