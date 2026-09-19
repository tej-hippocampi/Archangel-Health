"""The onboarding curriculum. This does not enable specialties in paid queues.

One distinct practice/examination pair per launch specialty. Topics constrain
authoring and evidence retrieval; they are not answer keys or clinical approval.
Only cases that pass the independent evidence gates enter the release bundle.
"""
from asclepius.onboarding_specialties import canonical

# (display name, practice topic, examination topic). Keep each pair clinically
# distinct so practice cannot reveal the examination's decision.
CURRICULUM = (
    ("Family Medicine", "adult hypertension diagnosis using out-of-office measurements", "adult acute low back pain red flags and imaging stewardship"),
    ("Internal Medicine", "iron deficiency anemia diagnostic evaluation", "venous thromboembolism diagnosis using pretest probability"),
    ("Pediatrics", "infant bronchiolitis supportive care", "febrile infant risk stratification"),
    ("Geriatrics", "delirium assessment and reversible causes", "falls assessment and medication review in older adults"),
    ("Cardiology", "atrial fibrillation stroke prevention", "heart failure with reduced ejection fraction therapy"),
    ("Pulmonology", "asthma controller treatment", "chronic obstructive pulmonary disease exacerbation assessment"),
    ("Gastroenterology", "celiac disease diagnostic testing", "acute upper gastrointestinal bleeding initial management"),
    ("Nephrology", "chronic kidney disease albuminuria and renoprotective therapy", "acute kidney injury volume assessment and medication review"),
    ("Endocrinology", "primary hypothyroidism diagnosis", "primary hyperparathyroidism diagnostic evaluation"),
    ("Rheumatology", "rheumatoid arthritis early disease modifying therapy", "giant cell arteritis urgent assessment"),
    ("Hematology", "immune thrombocytopenia evaluation", "heparin induced thrombocytopenia diagnosis and immediate management"),
    ("Oncology", "febrile neutropenia immediate management", "immune checkpoint inhibitor colitis assessment"),
    ("Infectious Disease", "asymptomatic bacteriuria antibiotic stewardship", "Staphylococcus aureus bacteremia diagnostic evaluation"),
    ("Allergy & Immunology", "anaphylaxis first line treatment", "penicillin allergy evaluation and delabeling"),
    ("Neurology", "Guillain Barre syndrome initial treatment", "myasthenia gravis impending crisis assessment"),
    ("Dermatology", "atopic dermatitis topical treatment", "acne vulgaris systemic antibiotic stewardship"),
    ("General Surgery", "acute calculous cholecystitis management", "adhesive small bowel obstruction strangulation assessment"),
    ("Neurosurgery", "aneurysmal subarachnoid hemorrhage initial management", "cauda equina syndrome urgent evaluation"),
    ("Orthopedic Surgery", "open fracture initial management", "acute compartment syndrome recognition"),
    ("Cardiothoracic Surgery", "acute type A aortic dissection surgical assessment", "severe symptomatic aortic stenosis heart team assessment"),
    ("Vascular Surgery", "acute limb ischemia initial management", "symptomatic carotid stenosis assessment"),
    ("Plastic Surgery", "hand flexor tendon injury assessment", "burn injury referral and initial assessment"),
    ("Otolaryngology (ENT)", "sudden sensorineural hearing loss assessment", "adult neck mass diagnostic evaluation"),
    ("Urology", "obstructed infected ureteric stone urgent drainage", "visible hematuria diagnostic evaluation"),
    ("Ophthalmology", "acute angle closure glaucoma urgent management", "giant cell arteritis visual symptoms urgent management"),
    ("Colorectal Surgery", "acute diverticulitis selective antibiotic management", "rectal cancer staging before surgery"),
    ("Transplant Surgery", "kidney transplant graft dysfunction diagnostic evaluation", "liver transplant hepatic artery thrombosis assessment"),
    ("Obstetrics & Gynecology", "ectopic pregnancy initial assessment", "postpartum hemorrhage initial management"),
    ("Emergency Medicine", "acute pulmonary embolism risk stratification", "sepsis early recognition and management"),
    ("Anesthesiology", "malignant hyperthermia recognition and management", "local anesthetic systemic toxicity initial management"),
    ("Critical Care Medicine", "septic shock initial management", "acute respiratory distress syndrome lung protective ventilation"),
    ("Radiology", "iodinated contrast kidney risk assessment", "incidental pulmonary nodule follow up using a supplied structured CT report"),
    ("Pathology", "basal cell carcinoma histopathologic interpretation of supplied H&E micrograph", "invasive squamous cell carcinoma histopathologic interpretation of supplied H&E micrograph"),
    ("Nuclear Medicine", "thyroid scintigraphy indications and pregnancy precautions", "FDG PET CT preparation hyperglycemia and uptake interpretation using a structured report"),
    ("Psychiatry", "bipolar depression assessment before antidepressant treatment", "suicide risk assessment and safety planning"),
    ("Physical Medicine & Rehabilitation", "post stroke spasticity rehabilitation assessment", "spinal cord injury autonomic dysreflexia management"),
    ("Radiation Oncology", "palliative radiotherapy for painful bone metastases", "spinal cord compression urgent radiotherapy assessment"),
    ("Sports Medicine", "sports concussion return to play assessment", "acute lateral ankle sprain rehabilitation"),
    ("Pain Medicine", "chronic low back pain multimodal management", "opioid therapy risk mitigation and safe tapering"),
    ("Palliative Care", "refractory breathlessness symptom management", "goals of care shared decision making in serious illness"),
    ("Occupational Medicine", "occupational asthma exposure assessment", "occupational needlestick HIV exposure assessment"),
    ("Preventive Medicine", "colorectal cancer screening average risk adults", "lung cancer screening eligibility and shared decision making"),
    ("Medical Genetics", "Lynch syndrome genetic testing and counseling", "BRCA hereditary cancer germline variant counseling"),
)

SPECIALTIES = tuple(canonical(row[0]) for row in CURRICULUM)
assert len(SPECIALTIES) == len(set(SPECIALTIES)) == 43

# PubMed searches use concise disease terms, not the whole teaching instruction
# (which would AND words like "supplied" and "assessment" and return no papers).
_SEARCH_PAIRS = """
hypertension screening|low back pain
iron deficiency anemia|venous thromboembolism
bronchiolitis|febrile infant
delirium|falls older adults
atrial fibrillation|heart failure reduced ejection fraction
asthma|COPD exacerbation
celiac disease|upper gastrointestinal bleeding
chronic kidney disease albuminuria|acute kidney injury
hypothyroidism|primary hyperparathyroidism
rheumatoid arthritis|giant cell arteritis
immune thrombocytopenia|heparin induced thrombocytopenia
febrile neutropenia|immune checkpoint inhibitor colitis
asymptomatic bacteriuria|Staphylococcus aureus bacteremia
anaphylaxis|penicillin allergy
Guillain Barre syndrome|myasthenia gravis
atopic dermatitis|acne vulgaris
acute cholecystitis|adhesive small bowel obstruction
aneurysmal subarachnoid hemorrhage|cauda equina syndrome
open fracture|acute compartment syndrome
type A aortic dissection|aortic stenosis
acute limb ischemia|symptomatic carotid stenosis
flexor tendon injury|burn injury
sudden sensorineural hearing loss|adult neck mass
ureteral stone infection|hematuria
acute angle closure glaucoma|giant cell arteritis
acute diverticulitis|rectal cancer staging
kidney transplant dysfunction|liver transplant hepatic artery thrombosis
ectopic pregnancy|postpartum hemorrhage
pulmonary embolism|sepsis
malignant hyperthermia|local anesthetic systemic toxicity
septic shock|acute respiratory distress syndrome
iodinated contrast kidney|pulmonary nodule
basal cell carcinoma histology|cutaneous squamous cell carcinoma histopathology
thyroid scintigraphy|FDG PET hyperglycemia
bipolar depression|suicide prevention
stroke spasticity|autonomic dysreflexia
bone metastases radiotherapy|spinal cord compression radiotherapy
sports concussion|ankle sprain
chronic low back pain|opioid tapering
refractory breathlessness|goals of care
occupational asthma|occupational HIV exposure
colorectal cancer screening|lung cancer screening
Lynch syndrome|BRCA genetic counseling
""".strip().splitlines()
assert len(_SEARCH_PAIRS) == len(CURRICULUM)
SEARCH_TERMS = {topic: query for row, pair in zip(CURRICULUM, _SEARCH_PAIRS)
                for topic, query in zip(row[1:], pair.split("|"))}


def topic_for(specialty: str, kind: str) -> str | None:
    return next((row[1 if kind == "practice" else 2] for row in CURRICULUM
                 if canonical(row[0]) == canonical(specialty)), None)


def age_scope_for(specialty: str) -> str | None:
    specialty = canonical(specialty)
    if specialty not in SPECIALTIES:
        return None
    return "pediatric" if specialty == "pediatrics" else "older_adult" if specialty == "geriatrics" else "adult"
