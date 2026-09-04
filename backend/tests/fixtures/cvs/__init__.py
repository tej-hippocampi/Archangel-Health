"""Hand-written CVs, and what each one should produce.

The founders' walkthrough uploaded one real nephrology CV and the form came
back with three board certifications that did not exist and five fields left
blank that the document plainly stated. One sample is not enough to trust a
change like that, so the parser is held against a CORPUS: several CVs written
in different shapes, each paired with the exact fill it should produce.

Every fixture is a fictional person. Two rules govern the expectations, and
they are the module's own rules restated as tests:

  * a WRONG value is worse than a missing one. An empty box costs a physician
    ten seconds; a wrong one wears a "from your CV" chip they have to notice,
    disbelieve and correct, on a form asking them to vouch for their
    credentials.
  * nothing may be filled that the source text does not contain. Every
    expectation below is checkable by reading the CV above it.
"""

from __future__ import annotations

from typing import Any, Dict, List

#: (name, cv text, expected parse subset)
CV_FIXTURES: List[Dict[str, Any]] = []


def _fixture(name: str, text: str, expect: Dict[str, Any]) -> None:
    CV_FIXTURES.append({"name": name, "text": text.strip() + "\n", "expect": expect})


# ── The CV from the walkthrough ─────────────────────────────────────────────
_fixture(
    "us_md_nephrologist",
    """
Rachel A. Kessler, MD
Board-Certified Nephrologist
Chicago, Illinois | (312) 555-0148 | rachel.kessler.md@example.com | linkedin.com/in/rachelkesslermd

PROFESSIONAL SUMMARY
Board-certified nephrologist with 10 years of clinical experience in the diagnosis and
management of acute and chronic kidney disease, end-stage renal disease, and dialysis care.

LICENSURE & BOARD CERTIFICATION
Board Certified, Nephrology - American Board of Internal Medicine (ABIM), 2017; recertified 2027
Board Certified, Internal Medicine - American Board of Internal Medicine (ABIM), 2015
Active Medical License - State of Illinois, License #IL-0384521 (current)
Active Medical License - State of Ohio, License #OH-0472910 (current)
DEA Registration - Active, current

PROFESSIONAL EXPERIENCE
Lakeshore Nephrology & Hypertension Associates - Chicago, IL Aug 2021 - Present
Senior Attending Nephrologist
Manage a panel of 400+ outpatients with CKD stages 1-5.

Summit Kidney & Hypertension Specialists - Columbus, OH Jul 2017 - Jul 2021
Attending Nephrologist

Cleveland Clinic - Cleveland, OH Jul 2015 - Jun 2017
Fellow, Nephrology

Cleveland Clinic - Cleveland, OH Jul 2012 - Jun 2015
Resident, Internal Medicine

EDUCATION
University of Pittsburgh School of Medicine - Pittsburgh, PA 2008 - 2012
Doctor of Medicine (MD)
""",
    {
        "full_name": "Rachel A. Kessler",
        "degrees": ["MD"],
        "specialty": "nephrology",
        "specialty_display": "Nephrology",
        "years_in_practice": 10,
        "linkedin_url": "https://linkedin.com/in/rachelkesslermd",
        "npi": None,                       # the CV has none; inventing one is the failure
        "employer": "Lakeshore Nephrology & Hypertension Associates",
        "board_certifications_structured": [
            {"board": "ABIM", "specialty": "Nephrology"},
            {"board": "ABIM", "specialty": "Internal Medicine"},
        ],
        "licenses": [
            {"state": "IL", "number": "IL-0384521"},
            {"state": "OH", "number": "OH-0472910"},
        ],
        "training": [
            {"kind": "fellowship", "institution": "Cleveland Clinic", "end_year": "2017"},
            {"kind": "residency", "institution": "Cleveland Clinic", "end_year": "2015"},
        ],
        # The three the old parser invented, named so a regression is legible.
        "board_labels_must_not_contain": ["Nephrologist", "nephrologist with", "ABIM"],
    },
)

# ── A DO, a different board, a subspecialty ─────────────────────────────────
_fixture(
    "do_cardiologist",
    """
Marcus T. Oyelaran, DO
Interventional Cardiologist
Houston, Texas | marcus.oyelaran.do@example.com

CERTIFICATION AND LICENSURE
Board Certified, Cardiovascular Disease - American Board of Internal Medicine (ABIM), 2019
Board Certified, Internal Medicine - American Board of Internal Medicine (ABIM), 2015
Texas Medical License #TX-J8841 (active)

EXPERIENCE
Bayou Heart Institute - Houston, TX Sep 2020 - Present
Interventional Cardiologist

Baylor College of Medicine - Houston, TX Jul 2018 - Jun 2020
Fellow, Interventional Cardiology

Baylor College of Medicine - Houston, TX Jul 2012 - Jun 2015
Resident, Internal Medicine
""",
    {
        "full_name": "Marcus T. Oyelaran",
        "degrees": ["DO"],
        "specialty": "cardiology",
        "specialty_display": "Cardiology",
        "employer": "Bayou Heart Institute",
        "board_certifications_structured": [
            {"board": "ABIM", "specialty": "Cardiovascular Disease"},
            {"board": "ABIM", "specialty": "Internal Medicine"},
        ],
        "licenses": [{"state": "TX", "number": "TX-J8841"}],
        "training": [
            {"kind": "fellowship", "institution": "Baylor College of Medicine",
             "end_year": "2020"},
            {"kind": "residency", "institution": "Baylor College of Medicine",
             "end_year": "2015"},
        ],
    },
)

# ── Trained outside the US: no state licence, no US board ───────────────────
_fixture(
    "img_mbbs_oncologist",
    """
Priya Raghavan, MBBS, MD
Consultant Medical Oncologist
Bengaluru, India | priya.raghavan@example.com

QUALIFICATIONS
MBBS, Bangalore Medical College, 2006
MD (General Medicine), All India Institute of Medical Sciences, 2010

APPOINTMENTS
Sankara Cancer Centre - Bengaluru 2014 - Present
Consultant Medical Oncologist

Tata Memorial Hospital - Mumbai 2011 - 2014
Fellow, Medical Oncology
""",
    {
        "full_name": "Priya Raghavan",
        "employer": "Sankara Cancer Centre",
        # No US state licence appears anywhere. Inventing one on a form that
        # cross-checks against NPPES would be worse than an empty box.
        "licenses": [],
        # No board is named, and "MD (General Medicine)" is a degree.
        "board_certifications_structured": [],
        "training": [
            {"kind": "fellowship", "institution": "Tata Memorial Hospital",
             "end_year": "2014"},
        ],
    },
)

# ── Nothing much on it. Should fill almost nothing. ─────────────────────────
_fixture(
    "minimal_one_pager",
    """
Dana Whitfield, MD
Family physician, Portland

Experience: eight years in outpatient family medicine.
References available on request.
""",
    {
        "full_name": "Dana Whitfield",
        "degrees": ["MD"],
        "board_certifications_structured": [],
        "licenses": [],
        "training": [],
        "employer": "",
        "npi": None,
    },
)

# ── Long academic CV: publications must not become credentials ──────────────
_fixture(
    "academic_long_cv",
    """
Helen O. Barros, MD, PhD
Professor of Medicine

BOARD CERTIFICATION
Board Certified, Nephrology - American Board of Internal Medicine (ABIM), 2004

TRAINING
Massachusetts General Hospital - Boston, MA 2000 - 2003
Fellow, Nephrology

PUBLICATIONS
Barros HO, et al. "Cardiology of the uremic patient." Kidney International, 2019.
Barros HO. "Oncology referral patterns in dialysis." J Am Soc Nephrol, 2018.
Barros HO, et al. "Dermatology manifestations in ESRD." Am J Kidney Dis, 2017.
Barros HO. "Emergency medicine and the dialysis patient." Ann Emerg Med, 2016.
""",
    {
        "full_name": "Helen O. Barros",
        # ONE certification. The publication titles name four other specialties
        # and none of them is a credential this physician holds.
        "board_certifications_structured": [
            {"board": "ABIM", "specialty": "Nephrology"},
        ],
        "specialty_display": "Nephrology",
        "training": [
            {"kind": "fellowship", "institution": "Massachusetts General Hospital",
             "end_year": "2003"},
        ],
        "licenses": [],
    },
)

# ── A labelled NPI, and a phone number that must not be read as one ─────────
_fixture(
    "labelled_npi_and_a_phone",
    """
Samuel Adeyemi, MD
Phone: (415) 555 0182
Fax: 4155550183
NPI: 1234567893

Board Certified, Emergency Medicine - American Board of Emergency Medicine (ABEM), 2016
California Medical License #CA-A44219 (current)
""",
    {
        "full_name": "Samuel Adeyemi",
        "npi": "1234567893",
        "board_certifications_structured": [
            {"board": "ABEM", "specialty": "Emergency Medicine"},
        ],
        "licenses": [{"state": "CA", "number": "CA-A44219"}],
    },
)

# ── Two licences, one lapsed ────────────────────────────────────────────────
_fixture(
    "multi_licence",
    """
Nina Kowalski, MD

Active Medical License - State of New York, License #NY-118842 (current)
Medical License - State of Florida, License #FL-990211 (inactive)

Board Certified, Dermatology - American Board of Dermatology (ABD), 2013
""",
    {
        "full_name": "Nina Kowalski",
        "licenses": [
            {"state": "NY", "number": "NY-118842", "current": "yes"},
            {"state": "FL", "number": "FL-990211", "current": ""},
        ],
        "board_certifications_structured": [
            {"board": "ABD", "specialty": "Dermatology"},
        ],
    },
)

# ── Still in training: no board certification to claim ──────────────────────
_fixture(
    "resident_in_training",
    """
Tomas Lindqvist, MD
Internal Medicine Resident

University of Michigan - Ann Arbor, MI Jul 2023 - Present
Resident, Internal Medicine

EDUCATION
Karolinska Institutet 2017 - 2023
Doctor of Medicine
""",
    {
        "full_name": "Tomas Lindqvist",
        # Not certified yet, and a form claiming otherwise would be a false
        # credential on a record that gets sold.
        "board_certifications_structured": [],
        "licenses": [],
        "training": [
            {"kind": "residency", "institution": "University of Michigan",
             "end_year": None},
        ],
        # Their current post IS their training, which the training block
        # already records; "where do you practise" is not a residency.
        "employer": "",
    },
)

# ── "Board certified in X" with no issuer named ─────────────────────────────
_fixture(
    "no_board_named",
    """
Grace Achebe, MD
Board certified in Gastroenterology.
Practising in Atlanta since 2014.
""",
    {
        "full_name": "Grace Achebe",
        "board_certifications_structured": [
            {"board": "", "specialty": "Gastroenterology"},
        ],
        "licenses": [],
    },
)
