# Cursor PRD — Fix Patricia Alvarez Demo Data (T-96 Survey + Intake Form)

> **Model note:** This is written for Composer 2 Fast. Every step is explicit — exact files, exact functions, exact values. Do not improvise schemas; copy the values given. Do NOT refactor anything outside the two functions named below.

## Background (read once)

Patricia Alvarez is a seeded pre-op demo patient in the **TRIAGEDM** tenant. Her tier evolution (Initial **Tier 1 → Current Tier 2**) and the bullet-point "why" card already work and look correct — **do not change those.** Two drill-downs are broken:

1. Clicking the **red T-96 box** opens a modal that says *"Survey window closed; no response on file"* with every answer blank and all scores `0`. It should show her completed, low-scoring survey.
2. Clicking **"View Intake Form"** does nothing (button disabled / "No intake form available yet"). It should open her completed intake form, with the **PAM section scored very low** and every other section benign (Tier-1-looking).

Both are caused by the seed writing data in the wrong shape / wrong key. The fixes are entirely inside one file: **`backend/triage_demo_seed.py`**, function **`_seed_patricia_extras(team_store, open_d)`** (around line 515). No frontend changes are needed — the reader code already works once the data is correct.

---

## Part A — Fix the T-96 readiness survey

### Why it's broken (root cause, verified)

- The reader is `backend/main.py` → `_build_preop_window_detail()` (~line 2234). For window `t96` it computes `day = WINDOW_SURVEY_DAY["t96"]` and calls `team_store.get_survey_response(patient_id, day, survey_type="preop")`.
- `WINDOW_SURVEY_DAY` is defined in **`backend/preop_survey.py` line 10**: `{"t96": -4, "t48": -2, "t24": -1}`. So the reader looks for **survey_day = -4**.
- The current seed saves with **`survey_day=-96`** → the reader finds nothing → "no response on file."
- The current seed also saves answers as `{"question_index": i, "response": "Not Clear"}`. The reader (`_preop_window_answer_map` in main.py ~line 2224) and the scorer (`score_preop_survey` in `backend/preop_survey.py` line 405) key answers by **`a["id"]`** (the question id) and require **valid option strings**. `question_index` and `"Not Clear"` are both invalid → every answer renders `—` and the score is `0`.

### What to change

In `backend/triage_demo_seed.py`, at the top of the file add this import (next to the existing imports):

```python
from preop_survey import WINDOW_SURVEY_DAY, score_preop_survey, parse_surgery_datetime
```

Then in `_seed_patricia_extras(...)`, **replace** the current `preop_answers = [...]` + `save_survey_response(...survey_day=-96...)` block with the following. These answer `id`s are the real T-96 question ids from `backend/preop_survey.py` (`T96_QUESTIONS`, lines 39–104). The chosen responses are the lowest-scoring valid option for each question, which produces a RED tier:

```python
# Real T-96 question ids (see preop_survey.py T96_QUESTIONS). Low-scoring answers → RED.
patricia_t96_answers = [
    {"id": "t96_anxiety_proc",       "response": "5"},                 # most anxious → 25
    {"id": "t96_anxiety_anesthesia", "response": "5"},                 # most anxious → 25
    {"id": "t96_understand_proc",    "response": "Strongly Disagree"}, # → 0
    {"id": "t96_who_to_call",        "response": "Disagree"},          # red flag → 0
    {"id": "t96_meds_confirmed",     "response": "No"},                # red flag → 0
    {"id": "t96_ride",               "response": "No"},                # red flag → 0
    {"id": "t96_caregiver_24h",      "response": "No"},                # red flag → 0
    {"id": "t96_supplies",           "response": "No"},                # red flag → 0
]

# Score against the patient's real surgery date so the modal's breakdown matches.
_sd = (patient_store_blob or {}).get("structured_data") or {}   # see note below
_surgery_dt = parse_surgery_datetime(_sd.get("procedure_date") or "")
_scored = score_preop_survey("t96", patricia_t96_answers, _surgery_dt, _sd) if _surgery_dt else {}

team_store.save_survey_response(
    patient_id=pid,
    survey_day=WINDOW_SURVEY_DAY["t96"],          # -4, NOT -96
    answers=patricia_t96_answers,
    score=_scored.get("survey_score"),
    tier="RED",
    submitted_at=_dt_combine(open_d - timedelta(days=4)),
    survey_type="preop",
)
```

**Note on `_sd`:** `_seed_patricia_extras` currently only receives `team_store` and `open_d`. It needs the patient's `structured_data` to read `procedure_date`. Do the simplest thing: change the function signature to `_seed_patricia_extras(team_store, patient_store, open_d)` and update its one call site (search the file for `_seed_patricia_extras(` — it's called around line 860 inside the post-op/pre-op seeding loop). Inside the function set `patient_store_blob = patient_store.get("triage_patricia_alvarez")`. If passing `patient_store` is awkward, instead hardcode the surgery date you already seed for her and build `_surgery_dt` from it — but reading from `patient_store` is cleaner. Either way `score` is cosmetic; the modal recomputes from `answers`, so if in doubt just pass `score=6.25`.

### Acceptance for Part A

Restart backend, log into TRIAGEDM, open Patricia Alvarez, click the **red T-96 box**. You must now see:
- Status message: **"Survey completed."** (not "window closed").
- **Score breakdown** with Tier: **red**, a non-zero survey score (~6), and flags listing the red-flag items.
- **Survey responses** populated with her answers (anxiety 5/5, "Strongly Disagree", "No"s), with the red-flag answers highlighted red.

---

## Part B — Make "View Intake Form" open her completed intake (PAM scored very low)

### Why it's broken (root cause, verified)

- The button handler `openDoctorIntakeForm(patient)` in `frontend/doctor.html` (~line 2839) requires `patient.intakeFormId` and fetches `/api/intake-forms/{intakeFormId}`.
- `intakeFormId` is set in `backend/main.py` line ~1842 from `team_store.get_latest_intake_form_for_patient(pid)`, which reads the **`intake_forms`** table.
- The seed currently only calls `save_preop_intake_submission(...)` — that writes to a **different** table (`preop_intake_submissions`). It never creates a row in `intake_forms`. So `intakeFormId` is `None`, the button is disabled, and clicking shows "No intake form available yet."

### What to change

Still inside `_seed_patricia_extras(...)` in `backend/triage_demo_seed.py`, **after** the survey/PAM/retier seeding, create a real, completed intake form. Use the canonical 11-section schema builder from the parser so the doctor modal renders every section.

Add this import at the top of the file:

```python
from intake_form_parser import _schema, _set_field
```

Then add this block inside `_seed_patricia_extras(...)`:

```python
import uuid

# Build the full 11-section intake schema, then fill it.
form_data = _schema()

# --- Benign, Tier-1-looking clinical answers (everything calm EXCEPT PAM) ---
_set_field(form_data, "section1_demographics", "fullLegalName", "Patricia Alvarez", "patient_record")
_set_field(form_data, "section1_demographics", "dateOfBirth", "1958-07-14", "patient_record")
_set_field(form_data, "section1_demographics", "sexAssignedAtBirth", "Female", "patient_record")
_set_field(form_data, "section1_demographics", "primaryLanguage", "English", "patient_record")

_set_field(form_data, "section2_surgicalInfo", "scheduledProcedure", "Total Hip Arthroplasty", "prep_document")
_set_field(form_data, "section2_surgicalInfo", "surgicalSite", "Right hip", "prep_document")

# Section 3 — no significant comorbidities (Tier-1 clinical picture)
_set_field(form_data, "section3_medicalHistory", "hypertension", False, "interview")
_set_field(form_data, "section3_medicalHistory", "diabetes", False, "interview")
_set_field(form_data, "section3_medicalHistory", "heartDisease", False, "interview")
_set_field(form_data, "section3_medicalHistory", "lungDisease", False, "interview")
_set_field(form_data, "section3_medicalHistory", "bleedingClottingDisorders", False, "interview")
_set_field(form_data, "section3_medicalHistory", "cancer", False, "interview")

# Section 6 — the one elevated soft factor that matches her "why" bullet:
#   "Intake form flagged: BMI 38, current smoker"
_set_field(form_data, "section6_socialHistory", "tobaccoUse", "Current", "interview")
form_data["section6_socialHistory"]["tobaccoUse"]["status"] = "Current"
form_data["section6_socialHistory"]["tobaccoUse"]["packYears"] = "10"
_set_field(form_data, "section6_socialHistory", "alcoholUse", "None", "interview")
_set_field(form_data, "section6_socialHistory", "postOpCaregiverAvailable", True, "interview")

# Section 9 — functionally independent (Tier-1)
_set_field(form_data, "section9_functionalAssessment", "functionalCapacityMETs", ">4 METs", "interview")
_set_field(form_data, "section9_functionalAssessment", "fallRisk", False, "interview")

# Section 10 — DAY-OF READINESS + PAM-13 PROXY.
# PAM items pam_1..pam_13 are a 4-point Likert ("1".."4"); 1 = lowest activation.
# Set ALL of them to "1" → raw_average 1.0 → activation_score 0 → level LOW.
# (Scoring lives in triage/preop_retier/pam_proxy.py:score_pam; needs >=10 scored items.)
for i in range(1, 14):
    _set_field(form_data, "section10_dayOfSurgeryReadiness", f"pam_{i}", "1", "interview")
# Keep the rest of section 10 benign
_set_field(form_data, "section10_dayOfSurgeryReadiness", "transportationArranged", True, "interview")
_set_field(form_data, "section10_dayOfSurgeryReadiness", "responsibleAdultPostOp", True, "interview")
_set_field(form_data, "section10_dayOfSurgeryReadiness", "npoStatusUnderstood", True, "interview")

_set_field(form_data, "section11_acknowledgments", "informationAccurate", True, "patient")

# Create the intake_forms row and mark it completed so the doctor modal opens.
_intake_id = uuid.uuid4().hex
team_store.create_intake_form(
    intake_form_id=_intake_id,
    patient_id=pid,
    surgery_id=None,
    status="COMPLETED",
    form_data=form_data,
)
team_store.update_intake_form_payload(
    _intake_id,
    form_data=form_data,
    red_flags=[],
    conflicts=[],
    status="COMPLETED",
    completed_at=_dt_combine(open_d - timedelta(days=2)),
    submitted_at=_dt_combine(open_d - timedelta(days=2)),
)
```

### Keep the existing PAM assessment consistent

The seed already calls `team_store.save_pam_assessment(... level="LOW", activation_score=22.0 ...)`. Leave it, but for internal consistency with the all-`"1"` answers set **`raw_sum=13, items_scored=13, raw_average=1.0, activation_score=0.0, level="LOW"`**. This matches `score_pam`'s output for thirteen `"1"` responses and keeps the demo defensible if anyone inspects it.

### Acceptance for Part B

Restart backend, open Patricia Alvarez, click **"View Intake Form"**. You must now see:
- The doctor intake modal opens (no "No intake form available" toast; button is enabled).
- Sections 1–11 render. Clinical sections look benign (no comorbidities, independent function).
- **Section 10 (Day Of Surgery Readiness)** shows `pam_1`…`pam_13` all = **"1"** — i.e. a clearly low PAM activation.
- The roster row / detail header for Patricia shows **Intake Status: COMPLETED** (it reads `latest_intake.status`).

---

## Part C — Do-not-break checklist

- Do **not** change Patricia's tier values (Initial Tier 1, Current Tier 2) or the three "why" bullets (`T96_READINESS_RED`, `INTAKE_BMI_SMOKER`, `PAM_LEVEL_LOW`). Those already render correctly and the user likes them.
- Do **not** touch any other seeded patient, the CDRSNAI1 tenant, or `manan.vyas@cedarssinai.com`.
- Do **not** edit `backend/main.py`, `backend/preop_survey.py`, or `backend/intake_form_parser.py` logic — only **import** from them. The readers already work; the bug is only in the seed data.
- All edits are confined to `backend/triage_demo_seed.py`, function `_seed_patricia_extras` (and its single call site for the new `patient_store` argument).

## Part D — Verify before declaring done

1. Restart the backend (in-memory + SQLite reseed). If `DEMO_SEED_STRATEGY` defaults to `preserve`, run once with `DEMO_SEED_STRATEGY=reset` so Patricia's old bad rows are cleared (`_clear_triage_sqlite` already deletes `survey_responses`, `pam_assessments`, `preop_intake_submissions`, and you should add `intake_forms` deletion for her id to that helper if it isn't already covered).
2. Open Patricia → red T-96 box → confirm Part A acceptance.
3. Open Patricia → View Intake Form → confirm Part B acceptance.
4. Run `python3 -m pytest backend/tests/test_triage_demo.py -q` and report results.

If you cannot complete a step, stop and report which one — do not invent alternative survey day numbers or answer keys.
