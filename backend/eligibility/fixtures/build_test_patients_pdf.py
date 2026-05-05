"""
Generates a multi-page PDF with 5 synthetic test patients for the TEAM
eligibility feature. Each patient page is shaped like an EHR export so the
extraction pipeline can lift demographics, insurance/eligibility fields, and
pre-op instructions.

Patient mix:
  1. Margaret O'Sullivan  - ELIGIBLE             (LEJR / TKA)
  2. Robert Hayes         - INELIGIBLE: not_ma   (HIP_FEMUR, MAPD H1036)
  3. Patricia Lin         - INELIGIBLE: ESRD     (SPINAL_FUSION)
  4. James Whitfield      - INELIGIBLE: UMWA     (CABG)
  5. Dorothy Chen         - ELIGIBLE             (MAJOR_BOWEL)

All data is synthetic. MBIs match the CMS regex but do not belong to any real
beneficiary.
"""
from __future__ import annotations
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)

OUTPUT = "team_eligibility_test_patients.pdf"

# -----------------------------------------------------------------------------
# Patient records
# -----------------------------------------------------------------------------

PATIENTS = [
    # -------------------------------------------------------------------------
    {
        "expected_verdict": "ELIGIBLE",
        "name": "Margaret O'Sullivan",
        "dob": "1953-04-12",
        "age": 72,
        "sex": "Female",
        "mrn": "MRN-100231",
        "mbi": "1EG4TE5MK73",
        "address": "412 Elmwood Ave, Cleveland, OH 44102",
        "phone": "(216) 555-0142",
        "pcp": "Dr. Helena Brooks, MD - Cleveland Internal Medicine",
        "anchor_procedure": "LEJR",
        "procedure_long": "Right Total Knee Arthroplasty (Total Knee Replacement)",
        "surgery_date": "2026-06-15",
        "surgeon": "Dr. Anil Krishnan, MD - Orthopedic Surgery",
        "hospital": "MetroHealth Main Campus, Cleveland OH",
        "history": [
            "Osteoarthritis, right knee (M17.11) - severe, refractory to conservative care",
            "Hypertension, controlled (I10) - lisinopril 20 mg daily",
            "Hyperlipidemia (E78.5) - atorvastatin 40 mg nightly",
            "GERD (K21.9) - omeprazole 20 mg daily",
            "BMI 28.4 - overweight, stable",
            "Cataract extraction OD (2022) - uneventful",
        ],
        "medications": [
            "Lisinopril 20 mg PO daily",
            "Atorvastatin 40 mg PO QHS",
            "Omeprazole 20 mg PO daily",
            "Acetaminophen 500 mg PO Q6H PRN pain",
        ],
        "allergies": "Penicillin (rash). No latex allergy.",
        "social": "Retired schoolteacher. Lives with spouse. Non-smoker. Occasional wine.",
        "labs": [
            "HbA1c 5.4% (2026-04-02)",
            "BMP within normal limits (2026-04-02)",
            "Hgb 13.2 g/dL, Plt 248k (2026-04-02)",
            "ECG: NSR, no ST changes (2026-04-15)",
        ],
        # Insurance / eligibility - shape so the LLM can lift each TEAM field.
        "insurance": {
            "primary_payer": "Medicare (Original / Fee-for-Service)",
            "part_a_status": "ACTIVE",
            "part_a_effective": "2018-04-01",
            "part_a_termination": "(no termination date on file)",
            "part_b_status": "ACTIVE",
            "part_b_effective": "2018-04-01",
            "part_b_termination": "(no termination date on file)",
            "part_c_ma_enrolled": "NO - patient is enrolled in Original Medicare. No Part C / Medicare Advantage plan on file.",
            "ma_contract_id": "(none)",
            "msp_indicator": "NO - Medicare is the primary payer. No MSP record on file (no workers comp, no automobile liability, no employer group health plan as primary, no black lung).",
            "secondary_payer": "AARP Medicare Supplement Plan G (UnitedHealthcare) - secondary only",
            "eligibility_basis": "AGE (entitled at 65)",
            "esrd_indicator": "NO - patient does not have End-Stage Renal Disease. No dialysis. No ESRD entitlement on file.",
            "umwa_indicator": "NO - patient is not enrolled in the United Mine Workers of America Health Plan.",
        },
        "preop_instructions": [
            "STOP eating solid food at midnight the night before surgery (NPO after midnight).",
            "Clear liquids permitted up to 2 hours before scheduled arrival.",
            "HOLD lisinopril on the morning of surgery. Resume POD #1 unless otherwise instructed.",
            "Continue atorvastatin and omeprazole as scheduled, including morning of surgery (sip of water).",
            "Shower with chlorhexidine (CHG) 4% wash the night before AND morning of surgery. Do not apply lotions, deodorants, or perfumes.",
            "Arrive at MetroHealth Main Campus pre-op holding at 0530 on 2026-06-15 (case time 0730).",
            "Bring CPAP if used at home, current medication list, photo ID, insurance card.",
            "Arrange a responsible adult driver and 24-hour postoperative companion.",
            "Pre-op PT visit completed 2026-05-20: home set up for safe ambulation, raised toilet seat in place.",
            "Anticoagulation plan: aspirin 81 mg PO daily x 35 days starting POD #1 (DVT prophylaxis).",
        ],
    },
    # -------------------------------------------------------------------------
    {
        "expected_verdict": "INELIGIBLE (Medicare Advantage)",
        "name": "Robert Hayes",
        "dob": "1955-09-22",
        "age": 70,
        "sex": "Male",
        "mrn": "MRN-100447",
        "mbi": "2W4FG7HK24",
        "address": "88 Lakeshore Dr, Tampa, FL 33606",
        "phone": "(813) 555-0987",
        "pcp": "Dr. Carlos Mendoza, MD - Tampa Bay Primary Care",
        "anchor_procedure": "HIP_FEMUR",
        "procedure_long": "Left Intertrochanteric Femur Fracture - ORIF (Cephalomedullary Nail)",
        "surgery_date": "2026-06-20",
        "surgeon": "Dr. Sarah Whitman, MD - Orthopedic Trauma",
        "hospital": "Tampa General Hospital",
        "history": [
            "Mechanical fall at home 2026-06-18, LEFT hip pain, unable to bear weight",
            "Closed left intertrochanteric femur fracture (S72.142A) - confirmed on plain film + CT",
            "Type 2 Diabetes Mellitus (E11.9) - HbA1c 6.8%, on metformin",
            "Coronary artery disease s/p DES to LAD 2019 - on dual antiplatelet completed 2020",
            "Atrial fibrillation, paroxysmal - on apixaban (HELD on admission)",
            "Hypertension - amlodipine 10 mg daily",
        ],
        "medications": [
            "Metformin 1000 mg PO BID",
            "Amlodipine 10 mg PO daily",
            "Apixaban 5 mg PO BID (HELD - last dose 2026-06-18 0800)",
            "Atorvastatin 80 mg PO QHS",
            "Acetaminophen 1 g PO Q6H scheduled",
            "Oxycodone 5 mg PO Q4H PRN severe pain",
        ],
        "allergies": "NKDA",
        "social": "Retired electrician. Lives alone in single-story home. Former smoker (quit 2010).",
        "labs": [
            "HbA1c 6.8% (2026-05-30)",
            "Cr 1.0 mg/dL, GFR 78 (2026-06-18)",
            "Hgb 11.4 g/dL, Plt 195k (2026-06-18)",
            "INR 1.1 (off apixaban x 48h)",
            "ECG: paroxysmal AFib, rate-controlled",
        ],
        # Eligibility section - clearly NOT TEAM eligible due to MA plan.
        "insurance": {
            "primary_payer": "Humana Gold Plus HMO (Medicare Advantage / Part C)",
            "part_a_status": "ACTIVE (administered through Medicare Advantage)",
            "part_a_effective": "2020-10-01",
            "part_a_termination": "(no termination date)",
            "part_b_status": "ACTIVE (administered through Medicare Advantage)",
            "part_b_effective": "2020-10-01",
            "part_b_termination": "(no termination date)",
            "part_c_ma_enrolled": "YES - Patient is enrolled in HUMANA GOLD PLUS HMO, a Medicare Advantage Prescription Drug (MAPD) plan. MA enrollment is active as of surgery date.",
            "ma_contract_id": "H1036 (Humana Medical Plan, Inc. - Florida HMO)",
            "ma_plan_name": "Humana Gold Plus HMO",
            "ma_effective_date": "2024-01-01",
            "msp_indicator": "N/A - patient receives Medicare benefits through Part C plan. Plan-level MSP rules apply.",
            "secondary_payer": "(none)",
            "eligibility_basis": "AGE (entitled at 65; enrolled in MA at 65)",
            "esrd_indicator": "NO - no End-Stage Renal Disease. No dialysis history.",
            "umwa_indicator": "NO - not enrolled in UMWA Health Plan.",
        },
        "preop_instructions": [
            "URGENT/EMERGENT case - target OR within 24-48h of fracture.",
            "NPO since 2026-06-18 2200; clear liquids until 2 hours pre-op.",
            "HOLD apixaban; last dose 2026-06-18 0800. Cleared by cardiology for surgery 2026-06-20.",
            "Continue amlodipine and atorvastatin morning of surgery with sip of water.",
            "Insulin sliding scale per inpatient protocol; HOLD metformin morning of surgery.",
            "CHG wipes night before and morning of surgery.",
            "Type and cross 2 units PRBC; have available in OR.",
            "DVT prophylaxis: pneumatic compression stockings on admission; restart apixaban POD #1 per surgeon.",
            "Geriatrics co-management consult - in place.",
            "Coordinate weight-bearing status and rehab placement at discharge.",
        ],
    },
    # -------------------------------------------------------------------------
    {
        "expected_verdict": "INELIGIBLE (ESRD-basis entitlement)",
        "name": "Patricia Lin",
        "dob": "1962-07-08",
        "age": 63,
        "sex": "Female",
        "mrn": "MRN-100612",
        "mbi": "3K8HJ9MN56",
        "address": "27 Maple Ridge Ln, San Jose, CA 95126",
        "phone": "(408) 555-0331",
        "pcp": "Dr. Yusuf Erkan, MD - South Bay Nephrology & IM",
        "anchor_procedure": "SPINAL_FUSION",
        "procedure_long": "Posterior Lumbar Interbody Fusion L4-L5 (PLIF)",
        "surgery_date": "2026-07-10",
        "surgeon": "Dr. Marcus Lefevre, MD - Spine Surgery",
        "hospital": "Stanford Health Care - Valley Specialty Center",
        "history": [
            "End-Stage Renal Disease (N18.6) - on hemodialysis MWF since 2020-08",
            "Medicare entitlement based on ESRD diagnosis (qualifying event 2020-05); patient is under 65",
            "Diabetes Mellitus Type 2 - longstanding, complicated by ESRD",
            "Hypertensive nephrosclerosis",
            "Lumbar spondylolisthesis L4-L5 with severe canal stenosis - failed 9 months conservative care",
            "Anemia of CKD - on darbepoetin",
            "Secondary hyperparathyroidism - on cinacalcet",
        ],
        "medications": [
            "Insulin glargine 24 units SubQ QHS",
            "Insulin aspart sliding scale with meals",
            "Amlodipine 10 mg PO daily",
            "Carvedilol 12.5 mg PO BID",
            "Sevelamer 800 mg PO TID with meals",
            "Cinacalcet 30 mg PO daily",
            "Darbepoetin alfa 60 mcg SubQ weekly (dialysis days)",
            "Atorvastatin 40 mg PO QHS",
        ],
        "allergies": "Iodinated contrast (hives). NKDA otherwise.",
        "social": "Retired accountant. Lives with daughter. Non-smoker. No alcohol.",
        "labs": [
            "Pre-dialysis BUN 62, Cr 7.4 mg/dL (2026-07-01)",
            "K 5.0, Phos 5.6, Ca 8.9 (2026-07-01)",
            "Hgb 10.3 g/dL on darbepoetin (2026-07-01)",
            "iPTH 412 pg/mL (2026-06-15)",
            "HbA1c 7.4% (2026-06-10)",
            "Dialysis access: left forearm AVF, mature, in use",
        ],
        # Insurance section - clearly ESRD-basis entitlement.
        "insurance": {
            "primary_payer": "Medicare (Original / Fee-for-Service)",
            "part_a_status": "ACTIVE",
            "part_a_effective": "2020-08-01",
            "part_a_termination": "(no termination date)",
            "part_b_status": "ACTIVE",
            "part_b_effective": "2020-08-01",
            "part_b_termination": "(no termination date)",
            "part_c_ma_enrolled": "NO - Original Medicare. No Part C / Medicare Advantage on file.",
            "ma_contract_id": "(none)",
            "msp_indicator": "NO - Medicare is primary. ESRD 30-month coordination period ended 2023-02; Medicare has been primary since.",
            "secondary_payer": "(none active)",
            "eligibility_basis": "ESRD (End-Stage Renal Disease) - patient qualifies for Medicare BECAUSE OF ESRD, not age or disability. Eligibility basis on CWF: ESRD entitlement effective 2020-08-01. ESRD is the qualifying condition.",
            "esrd_indicator": "YES - Eligibility basis: ESRD. Patient on chronic hemodialysis 3x/week. ESRD entitlement is the pathway by which patient receives Medicare. (Note: this is distinct from ESRD as a comorbidity in an age-entitled beneficiary.)",
            "umwa_indicator": "NO - not enrolled in UMWA Health Plan.",
        },
        "preop_instructions": [
            "Schedule dialysis the day before surgery (2026-07-09). NO dialysis morning of surgery.",
            "NPO after midnight. Clear liquids permitted up to 2 hours pre-op.",
            "HOLD insulin glargine to half-dose (12 units) the night before; HOLD all aspart on morning of surgery.",
            "HOLD amlodipine and carvedilol morning of surgery; resume POD #1 per anesthesia.",
            "Continue sevelamer with any oral intake; HOLD on day of surgery.",
            "Protect left forearm AVF: NO blood pressures, IVs, or blood draws on left arm. Sign on bed.",
            "Renal anesthesia consult required. Avoid nephrotoxic agents (NSAIDs, IV contrast, aminoglycosides).",
            "Type and cross 2 units PRBC. Anemia management: target Hgb >10 pre-op.",
            "CHG wipes night before AND morning of surgery.",
            "Arrange post-op dialysis on POD #1 or #2 per nephrology.",
            "Discharge planning: coordinate outpatient dialysis at usual center.",
        ],
    },
    # -------------------------------------------------------------------------
    {
        "expected_verdict": "INELIGIBLE (UMWA Health Plan)",
        "name": "James Whitfield",
        "dob": "1948-11-30",
        "age": 77,
        "sex": "Male",
        "mrn": "MRN-100819",
        "mbi": "4PQ7RT8VW01",
        "address": "1402 Coal Ridge Rd, Beckley, WV 25801",
        "phone": "(304) 555-0264",
        "pcp": "Dr. Linda Hargrove, MD - Beckley Family Medicine",
        "anchor_procedure": "CABG",
        "procedure_long": "Coronary Artery Bypass Grafting x3 (LIMA-LAD, SVG-OM1, SVG-RCA)",
        "surgery_date": "2026-06-25",
        "surgeon": "Dr. Henry Okonkwo, MD - Cardiothoracic Surgery",
        "hospital": "WVU Heart and Vascular Institute, Morgantown WV",
        "history": [
            "Severe three-vessel coronary artery disease - cath 2026-05-29: LAD 90% prox, OM1 85%, RCA 80%",
            "Stable angina (CCS class III) on maximal medical therapy",
            "Hypertension (I10) - controlled",
            "Hyperlipidemia (E78.5)",
            "Type 2 Diabetes (E11.9) - HbA1c 7.0%",
            "Coal Workers' Pneumoconiosis (J60) - mild, stable on serial imaging",
            "Former coal miner x 32 years (Beckley area underground mining); retired 2014",
            "Tobacco use - former, quit 2008 (50 pack-years prior)",
        ],
        "medications": [
            "Aspirin 81 mg PO daily",
            "Atorvastatin 80 mg PO QHS",
            "Metoprolol succinate 100 mg PO daily",
            "Lisinopril 20 mg PO daily",
            "Metformin 1000 mg PO BID",
            "Nitroglycerin 0.4 mg SL PRN angina",
        ],
        "allergies": "NKDA",
        "social": "Retired underground coal miner (32 years). Married. Lives at home. Former smoker.",
        "labs": [
            "Hgb 13.8 g/dL, Plt 220k (2026-06-15)",
            "Cr 1.1 mg/dL, GFR 72 (2026-06-15)",
            "HbA1c 7.0% (2026-06-10)",
            "LDL 71, HDL 38, TG 142 (2026-06-10)",
            "ECG: NSR, old inferior Q waves",
            "Echo: LVEF 50%, mild MR, no significant valvular disease",
        ],
        # Insurance section - clearly UMWA enrollment.
        "insurance": {
            "primary_payer": "Medicare (Original / Fee-for-Service)",
            "part_a_status": "ACTIVE",
            "part_a_effective": "2013-12-01",
            "part_a_termination": "(no termination date)",
            "part_b_status": "ACTIVE",
            "part_b_effective": "2013-12-01",
            "part_b_termination": "(no termination date)",
            "part_c_ma_enrolled": "NO - Original Medicare. No Medicare Advantage / Part C plan on file.",
            "ma_contract_id": "(none)",
            "msp_indicator": "NO - Medicare is the primary payer.",
            "secondary_payer": "UMWA HEALTH AND RETIREMENT FUNDS - 1974 Pensioners Benefit Plan (UMWA Health Plan). Plan ID: UMWA-1974-RET. Effective 2014-01-01. Provides supplemental coverage for retired coal miners.",
            "eligibility_basis": "AGE (entitled at 65)",
            "esrd_indicator": "NO - patient does not have ESRD. No dialysis.",
            "umwa_indicator": "YES - patient is enrolled in the United Mine Workers of America Health Plan (UMWA 1974 Pensioners Benefit Plan) as a retired coal miner. Plan is active and confirmed via 270/271 inquiry on 2026-06-01.",
        },
        "preop_instructions": [
            "NPO after midnight 2026-06-24. Clear liquids until 2h pre-op.",
            "Continue aspirin 81 mg through day of surgery.",
            "HOLD metformin morning of surgery; resume POD #2 if eating.",
            "HOLD lisinopril morning of surgery; resume POD #1-2 per surgeon based on hemodynamics and renal function.",
            "Continue metoprolol and atorvastatin morning of surgery (sip of water).",
            "CHG wipes nightly x 3 nights pre-op AND morning of surgery (full body, avoid eyes/ears).",
            "Pre-op clipping (NOT shaving) of chest, abdomen, and bilateral legs morning of surgery.",
            "Pulmonary toilet: incentive spirometry teaching pre-op given coal workers' pneumoconiosis.",
            "Pulmonary function testing complete 2026-06-08: mild restrictive pattern, FEV1 78% predicted.",
            "Type and cross 2 units PRBC. Cell saver available intraop.",
            "Carotid duplex: <50% bilateral - cleared.",
            "Dental clearance: completed 2026-05-22, no active infection.",
            "Discharge planning: cardiac rehab referral; UMWA plan covers Phase II program.",
        ],
    },
    # -------------------------------------------------------------------------
    {
        "expected_verdict": "ELIGIBLE",
        "name": "Dorothy Chen",
        "dob": "1950-02-14",
        "age": 76,
        "sex": "Female",
        "mrn": "MRN-101032",
        "mbi": "5XB6CD7EF89",
        "address": "318 Cedar Hollow Rd, Asheville, NC 28803",
        "phone": "(828) 555-0119",
        "pcp": "Dr. Priya Ramaswamy, MD - Blue Ridge Internal Medicine",
        "anchor_procedure": "MAJOR_BOWEL",
        "procedure_long": "Laparoscopic Sigmoid Colectomy (for sigmoid adenocarcinoma, T2N0)",
        "surgery_date": "2026-07-05",
        "surgeon": "Dr. Olivia Bertrand, MD - Colorectal Surgery",
        "hospital": "Mission Hospital, Asheville NC",
        "history": [
            "Sigmoid adenocarcinoma (C18.7), T2N0 - found on screening colonoscopy 2026-05-12, biopsy confirmed 2026-05-15",
            "Hypertension (I10) - well-controlled",
            "Osteoporosis (M81.0) - on alendronate weekly",
            "Hypothyroidism (E03.9) - on levothyroxine, euthyroid",
            "Mild diverticulosis (incidental)",
            "Cholecystectomy 2008 (laparoscopic, uneventful)",
        ],
        "medications": [
            "Levothyroxine 75 mcg PO daily",
            "Amlodipine 5 mg PO daily",
            "Hydrochlorothiazide 12.5 mg PO daily",
            "Alendronate 70 mg PO weekly",
            "Calcium carbonate 600 mg + Vitamin D3 800 IU PO daily",
        ],
        "allergies": "Sulfa drugs (rash). NKDA otherwise.",
        "social": "Retired librarian. Widowed. Lives alone, has supportive daughter nearby. Non-smoker. Rare alcohol.",
        "labs": [
            "CEA 2.4 ng/mL (2026-06-20)",
            "CT chest/abd/pelvis: sigmoid mass, no metastases (2026-05-22)",
            "Staging MRI pelvis: T2N0 (2026-05-25)",
            "Hgb 12.6 g/dL, Plt 232k (2026-06-20)",
            "Albumin 4.0 g/dL (2026-06-20)",
            "TSH 1.8 (2026-04-10)",
        ],
        # Insurance section - clean original Medicare.
        "insurance": {
            "primary_payer": "Medicare (Original / Fee-for-Service)",
            "part_a_status": "ACTIVE",
            "part_a_effective": "2015-02-01",
            "part_a_termination": "(no termination date)",
            "part_b_status": "ACTIVE",
            "part_b_effective": "2015-02-01",
            "part_b_termination": "(no termination date)",
            "part_c_ma_enrolled": "NO - Original Medicare. No Part C / Medicare Advantage plan enrollment on file.",
            "ma_contract_id": "(none)",
            "msp_indicator": "NO - Medicare is the primary payer. No workers compensation, no automobile liability, no employer group health plan as primary, no black lung benefits.",
            "secondary_payer": "Aetna Medicare Supplement Plan G (Medigap) - secondary supplemental only; Medicare remains primary.",
            "eligibility_basis": "AGE (entitled at 65)",
            "esrd_indicator": "NO - patient does not have End-Stage Renal Disease. No dialysis history. (Renal function: Cr 0.9, GFR 70.)",
            "umwa_indicator": "NO - patient is not enrolled in the United Mine Workers of America Health Plan.",
        },
        "preop_instructions": [
            "ERAS (Enhanced Recovery After Surgery) protocol - colorectal pathway.",
            "Mechanical bowel prep with oral antibiotics 2026-07-04: Polyethylene glycol (4 L) afternoon/evening + neomycin 1 g + metronidazole 500 mg PO at 1300, 1400, and 2200.",
            "Clear liquids only on 2026-07-04 starting 0800.",
            "NPO from 2400 on 2026-07-04. Carbohydrate-loading drink (Ensure Pre-Surgery, 12 oz) at 0400 on 2026-07-05.",
            "HOLD hydrochlorothiazide morning of surgery; resume POD #1 if hemodynamically stable.",
            "Continue amlodipine, levothyroxine morning of surgery (sip of water).",
            "HOLD alendronate; do not resume until tolerating full diet and able to remain upright 30 min.",
            "CHG wipes night before AND morning of surgery.",
            "Pre-op nutrition consult complete; albumin >3.5, low malnutrition risk.",
            "DVT prophylaxis: SCDs on admission + enoxaparin 40 mg SubQ daily starting 2h pre-op.",
            "Arrange responsible adult to drive home; expect 3-4 day inpatient stay.",
            "Post-op: early ambulation POD #0 evening, oral diet advance per ERAS, multimodal non-opioid analgesia.",
        ],
    },
]


# -----------------------------------------------------------------------------
# PDF rendering
# -----------------------------------------------------------------------------

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=16, spaceAfter=4,
                    textColor=colors.HexColor("#0b3d91"))
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12, spaceBefore=10,
                    spaceAfter=4, textColor=colors.HexColor("#0b3d91"))
H3 = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=10.5,
                    spaceBefore=6, spaceAfter=2, textColor=colors.HexColor("#333"))
BODY = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9.5,
                      leading=12.5, alignment=TA_LEFT)
SMALL = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8.5,
                       leading=11, textColor=colors.HexColor("#444"))
META = ParagraphStyle("Meta", parent=styles["BodyText"], fontSize=8.5,
                      leading=11, textColor=colors.HexColor("#666"))


def kv_table(rows):
    """Two-column key/value table."""
    data = [[Paragraph(f"<b>{k}</b>", SMALL), Paragraph(str(v), SMALL)] for k, v in rows]
    t = Table(data, colWidths=[2.0 * inch, 4.6 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e0e0e0")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f6f8fb")),
    ]))
    return t


def bullet_list(items):
    return [Paragraph(f"&bull; {it}", BODY) for it in items]


def patient_page(p):
    flow = []
    # Banner
    flow.append(Paragraph("HARTLAND REGIONAL HEALTH SYSTEM", META))
    flow.append(Paragraph("Comprehensive Patient Eligibility &amp; Pre-Operative Summary",
                          META))
    flow.append(Spacer(1, 6))
    flow.append(Paragraph(p["name"], H1))
    flow.append(Paragraph(
        f"MRN {p['mrn']}  &nbsp;|&nbsp;  DOB {p['dob']} (age {p['age']})  &nbsp;|&nbsp;  Sex {p['sex']}",
        SMALL,
    ))
    flow.append(Paragraph(
        f"<i>Document type: EHR Eligibility &amp; Pre-Op Export &nbsp;|&nbsp; Generated for TEAM intake review</i>",
        META,
    ))
    flow.append(Spacer(1, 6))

    # Demographics
    flow.append(Paragraph("Demographics &amp; Contacts", H2))
    flow.append(kv_table([
        ("Address", p["address"]),
        ("Phone", p["phone"]),
        ("Primary Care Provider", p["pcp"]),
        ("MBI (Medicare Beneficiary Identifier)", p["mbi"]),
    ]))

    # Procedure
    flow.append(Paragraph("Scheduled Procedure", H2))
    flow.append(kv_table([
        ("Anchor procedure (TEAM category)", p["anchor_procedure"]),
        ("Procedure", p["procedure_long"]),
        ("Scheduled surgery date", p["surgery_date"]),
        ("Operating surgeon", p["surgeon"]),
        ("Facility", p["hospital"]),
    ]))

    # Insurance / Eligibility - this is the section that drives TEAM checks.
    ins = p["insurance"]
    flow.append(Paragraph("Insurance &amp; Medicare Eligibility (Coverage as of Surgery Date)", H2))
    flow.append(Paragraph(
        "<i>Source: Medicare Common Working File (CWF) eligibility response and "
        "internal payer verification. Reviewed by patient access on file date.</i>",
        META,
    ))
    flow.append(kv_table([
        ("Primary payer", ins["primary_payer"]),
        ("Part A status", ins["part_a_status"]),
        ("Part A effective date", ins["part_a_effective"]),
        ("Part A termination date", ins["part_a_termination"]),
        ("Part B status", ins["part_b_status"]),
        ("Part B effective date", ins["part_b_effective"]),
        ("Part B termination date", ins["part_b_termination"]),
        ("Part C / Medicare Advantage enrollment", ins["part_c_ma_enrolled"]),
        ("MA contract ID (if applicable)", ins["ma_contract_id"]),
        ("Medicare Secondary Payer (MSP) indicator", ins["msp_indicator"]),
        ("Secondary / supplemental payer", ins["secondary_payer"]),
        ("Eligibility basis (CWF entitlement reason)", ins["eligibility_basis"]),
        ("ESRD indicator", ins["esrd_indicator"]),
        ("UMWA Health Plan indicator", ins["umwa_indicator"]),
    ]))
    flow.append(Spacer(1, 4))
    flow.append(Paragraph(
        f"<b>Internal eligibility note:</b> Expected TEAM verdict on review = "
        f"<b>{p['expected_verdict']}</b>. (This line is included for QA testing only.)",
        SMALL,
    ))

    # Medical history
    flow.append(Paragraph("Active Problem List &amp; Medical History", H2))
    for item in p["history"]:
        flow.append(Paragraph(f"&bull; {item}", BODY))

    # Medications
    flow.append(Paragraph("Current Medications", H2))
    for item in p["medications"]:
        flow.append(Paragraph(f"&bull; {item}", BODY))

    # Allergies + social
    flow.append(Paragraph("Allergies", H2))
    flow.append(Paragraph(p["allergies"], BODY))
    flow.append(Paragraph("Social History", H2))
    flow.append(Paragraph(p["social"], BODY))

    # Labs
    flow.append(Paragraph("Recent Labs &amp; Studies", H2))
    for item in p["labs"]:
        flow.append(Paragraph(f"&bull; {item}", BODY))

    # Pre-op instructions
    flow.append(Paragraph("Pre-Operative Instructions", H2))
    flow.append(Paragraph(
        f"<i>The following instructions are specific to {p['procedure_long']} "
        f"on {p['surgery_date']}.</i>",
        META,
    ))
    for i, item in enumerate(p["preop_instructions"], 1):
        flow.append(Paragraph(f"<b>{i}.</b> {item}", BODY))

    flow.append(Spacer(1, 6))
    flow.append(Paragraph(
        "<i>End of patient record. All data is synthetic; for software testing only.</i>",
        META,
    ))
    return flow


def build():
    doc = SimpleDocTemplate(
        OUTPUT, pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.55 * inch, bottomMargin=0.55 * inch,
        title="TEAM Eligibility Test Patients (Synthetic)",
        author="Archangel Health QA",
    )
    story = []
    # Cover
    story.append(Paragraph("TEAM Eligibility - Synthetic Test Patient Bundle", H1))
    story.append(Paragraph(
        "Five synthetic patients designed to exercise all six TEAM eligibility checks. "
        "All names, MBIs, addresses, and clinical details are fictitious.",
        BODY,
    ))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Bundle Contents", H2))
    cover_rows = [
        ["#", "Patient", "Anchor Procedure", "Expected Verdict", "Failing Check"],
    ]
    fail_map = {
        "ELIGIBLE": "(none - all six PASS)",
        "INELIGIBLE (Medicare Advantage)": "not_ma",
        "INELIGIBLE (ESRD-basis entitlement)": "not_esrd_basis",
        "INELIGIBLE (UMWA Health Plan)": "not_umwa",
    }
    for i, p in enumerate(PATIENTS, 1):
        cover_rows.append([
            str(i), p["name"], p["anchor_procedure"],
            p["expected_verdict"], fail_map.get(p["expected_verdict"], "-"),
        ])
    cover = Table(cover_rows, colWidths=[0.3 * inch, 1.7 * inch, 1.4 * inch,
                                          2.1 * inch, 1.2 * inch])
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f6f8fb")]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(cover)
    story.append(Spacer(1, 10))
    story.append(Paragraph("How to use this bundle", H3))
    story.append(Paragraph(
        "Upload this single PDF in the doctor portal Add Patient flow under "
        "either <b>Single</b> (the system will scope to the first patient) or "
        "<b>Group</b> (preferred - the system fans out one patient per record "
        "section). Each patient page includes a CWF-style eligibility block "
        "with explicit Part A/B status and dates, MA enrollment, MSP indicator, "
        "ESRD basis, and UMWA flag - shaped so the extraction prompt can lift "
        "each field with a verbatim sourceExcerpt.",
        BODY,
    ))
    story.append(PageBreak())

    for i, p in enumerate(PATIENTS):
        story.extend(patient_page(p))
        if i < len(PATIENTS) - 1:
            story.append(PageBreak())

    doc.build(story)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    build()
