# Demo Mode Implementation Plan

## Context
The goal is to turn this app into a polished, self-contained demo that a doctor can walk investors/stakeholders through without needing to set up real patients or click through any onboarding. The demo should feel like a live, active clinic with real-looking data.

The app has two separate data layers that must be updated independently:
1. **Roster + Patient Details** → in-memory `_patient_store` (backend, seeded at startup)
2. **Analytics Charts/Stats** → hardcoded `ANALYTICS_PATIENTS` JS array in `frontend/doctor.html`
3. **Escalations/Surveys** → SQLite `team.db` (persists across restarts)

---

## Files to Modify

| File | What changes |
|------|-------------|
| `backend/main.py` | Replace `_seed_demo_patient_if_empty()` with full 50-patient seed; add `pcpReferralSent` to patient store + `/api/patients` response |
| `backend/auth.py` (startup inject) | Ensure demo doctor account always exists at startup |
| `frontend/doctor.html` | Update `ANALYTICS_PATIENTS` (50 patients, diverse procedures); update `renderCompliance()` to show "Y"/"N" from real patient data |

---

## Step 1: Demo Doctor Account

**Credentials:** `manan.vyas@cedarssinai.com` / `ArchangelDemo2024!`

In `backend/main.py`, add a `_ensure_demo_doctor()` function called at startup (alongside `_seed_demo_patient_if_empty()`). It should:
- Use `auth._get_users()` to check if the account already exists
- If not, call `auth.register_user(...)` then immediately call the onboard endpoint logic to set profile fields
- Profile: name=`"Dr. Manan Vyas"`, doctor_type=`"General Surgeon"`, hospital_affiliations=`"Cedars-Sinai Medical Center"`, clinic_code=`"CDRSNAI1"`

The doctor profile is stored in `auth_users.json` (disk-persisted), so this survives restarts.

---

## Step 2: 50-Patient Seed (`_seed_demo_patient_if_empty` replacement)

Replace the existing single-patient seed in `backend/main.py` with `_seed_full_demo_data()`.

### Patient List (50 patients)

All use `clinic_code = "CDRSNAI1"`.

**Special patients (exact names required):**
| Name | pipeline_type | Notes |
|------|--------------|-------|
| Tej Patel | `pre_op` | Laparoscopic Appendectomy; has pre-op resources populated |
| Arya Bhatia | `post_op` | Laparoscopic Cholecystectomy; episode open_date = today-3 (Day 4) |
| Thenuk Rodrigo | `post_op` | Inguinal Hernia Repair; has D14 survey submitted |

**Remaining 47 patients:** Mix of pre-op (~20) and post-op (~27) with diverse general surgery procedures:
- Appendectomy, Cholecystectomy, Colectomy, Hernia Repair (inguinal/umbilical/hiatal), Thyroidectomy, Whipple Procedure, Gastric Bypass, Splenectomy, Bowel Resection, Sigmoidectomy, Gastrectomy, Nissen Fundoplication, Adrenalectomy, Pancreatectomy

**Patient store record structure (per patient):**
```python
_patient_store[pid] = {
    "name": full_name,
    "phone": "+1 (310) 555-XXXX",  # fake phone
    "email": "first.last@email.com",
    "pipeline_type": "pre_op" | "post_op",
    "voice_audio_url": None,
    "avatar_url": None,
    "voice_script": "",
    "structured_data": {
        "patient_name": full_name,
        "procedure_name": procedure,
        "procedure_date": iso_date,  # varies per patient
        "procedure_status": "scheduled" | "completed",
        ...
    },
    "clinic_code": "CDRSNAI1",
    "resource_code": resource_code,
    "office_phone": "(310) 555-0100",
    "pcp_referral_sent": True | False,  # ~75% True
    "resources": { ... },  # see below
}
```

---

## Step 3: Resources

### Tej Patel (Pre-Op)
Populate `resources.preop` with a realistic HTML battlecard for Laparoscopic Appendectomy prep:
- `voice_script`: Brief pre-op explanation (~200 words)
- `battlecard_html`: HTML card with sections: What to Expect, Day Before Surgery, Day of Surgery, What to Bring, After Surgery Recovery

### Arya Bhatia (Post-Discharge)
Populate `resources.diagnosis` and `resources.treatment` with post-op Cholecystectomy content:
- Diagnosis card: explains what was done, what was found
- Treatment card: recovery timeline, pain meds, diet, activity restrictions, wound care, red flags

### Other Patients
Populate with minimal but realistic stub resources (at minimum, `battlecard_html` with the procedure name and basic care instructions). `hasResources` must be `True` so the roster shows them as ready.

---

## Step 4: SQLite Seed (Idempotent)

The SQLite seed must be idempotent. Add a check: if `team.db` has > 1 episode for clinic `"CDRSNAI1"`, skip the SQLite seed entirely.

### Episodes
For each of 50 patients, call `_team_store.ensure_episode(...)`. 

**Special case — Arya Bhatia (Day 4):**
Do NOT use `ensure_episode` for Arya. Instead, directly upsert into the `episodes` table with `open_date = (date.today() - timedelta(days=3)).isoformat()`.

### Event Logs (Engagement Metrics)
For post-op patients, insert a realistic mix of events:
- ~85% get `platform_opened`
- ~70% get `diagnosis_video_watched`
- ~65% get `treatment_video_watched`
- ~60% get `avatar_chat`
- Spread event timestamps across their episode days

For Thenuk Rodrigo specifically: insert a `survey_completed` event at episode day 14 (occurred_at = open_date + 13 days) with payload `{"survey_day": 14}`.

For pre-op patients: insert `platform_opened` and ~60% `preop_video_watched`.

### Survey Responses — Thenuk Rodrigo Only
Insert a Day 14 survey response into `survey_responses`:
```python
_team_store.save_survey_response(
    patient_id="demo_thenuk_001",
    survey_day=14,
    answers=[
        {"question_index": 1, "response": "Strongly Agree"},
        {"question_index": 2, "response": "Agree"},
        {"question_index": 3, "response": "Agree"},
        {"question_index": 4, "response": "Strongly Agree"},
        {"question_index": 5, "response": "Agree"},
    ],
    score=80.0,
    tier="green",
    submitted_at=(episode_open + timedelta(days=13)).isoformat() + "T14:30:00",
)
```

Also insert a `survey_sends` record for day 14 so the scheduler doesn't re-send.

### Escalations (~20)
Insert 20 escalations across ~12 of the post-op patients. Mix of tiers:
- ~4 Tier 1 (life-threatening keywords): chest pain, severe bleeding
- ~8 Tier 2 (urgent): wound drainage, fever, swelling  
- ~8 Tier 3 (important): confused about meds, no support at home

Each escalation includes a realistic `conversation_snapshot` (3-5 message turns between patient and AI companion).

About 10 of 20 should be `resolved=True`.

---

## Step 5: PCP Referral Sent Status

### Backend Change
Add `pcp_referral_sent: bool` to the patient store dict (already covered in Step 2). Set ~75% of patients to `True`.

Expose it in `/api/patients` response:
```python
patients.append({
    ...existing fields...,
    "pcpReferralSent": d.get("pcp_referral_sent", False),
})
```

### Frontend Change (`doctor.html`)
Update `renderCompliance()` at line ~1479:
```javascript
// Change:
<td><span class="status-chip pending">N</span></td>
// To:
<td><span class="status-chip ${p.pcpReferralSent ? 'ok' : 'pending'}">${p.pcpReferralSent ? 'Y' : 'N'}</span></td>
```

---

## Step 6: Analytics Section (`frontend/doctor.html`)

Update the `ANALYTICS_PATIENTS` array (lines 1005–1042) with:
- 50 entries matching the demo patient names/procedures
- Diverse procedures (not just Lumbar/Cervical Fusion — use the general surgery procedures)
- Realistic score distributions (D7: 60–95, D14: slightly higher, D30: slightly higher)
- ~28 post-op, ~22 pre-op
- Natural escalation distribution (Tier 1: rare, Tier 2: occasional, Tier 3: more common)
- Survey scores reflecting ~80% completion at D7, ~65% at D14, ~50% at D30
- pcpReferralSent field matching the backend seed

---

## Edge Cases & Risks

### 1. Patient store resets on every restart
**Risk:** The SQLite data persists but `_patient_store` is cleared. If seed inserts new episodes on restart, you'll get duplicates in `event_logs` and `escalations`.  
**Fix:** Check SQLite for existing demo data before inserting. Gate on: `if _team_store.get_episode("demo_tej_001") is not None: skip_sqlite_seed()`.

### 2. Arya's Day 4 requires direct DB insert
**Risk:** `ensure_episode()` always uses `date.today()` as the open_date, giving Day 1.  
**Fix:** For Arya's episode, bypass `ensure_episode` and directly execute:
```sql
INSERT OR IGNORE INTO episodes (patient_id, open_date, close_date, status, procedure_type, clinic_code, resource_code, created_at)
VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
```
with `open_date = today - 3`.

### 3. Thenuk's survey must appear on timeline
**Risk:** The timeline shows `survey_completed` markers from `event_logs`. If the event is missing or on the wrong day, nothing is clickable.  
**Fix:** The `occurred_at` for the survey event must be `open_date + 13 days + some time`, which maps to calendar Day 14 in the episode. Verify with the timeline query: it groups events by `(date(occurred_at) - date(open_date)).days + 1`.

### 4. Escalations reference patient IDs that must be in `_patient_store`
**Risk:** `GET /api/escalations` does `_patient_store.get(row["patient_id"])` to get the name. If the patient_id isn't in `_patient_store`, the escalation shows the raw ID instead of a name.  
**Fix:** Only use patient IDs from the 50-patient seed set for escalations.

### 5. Analytics uses different data than roster
**Risk:** Analytics page shows different patients/numbers than the actual roster, which looks inconsistent.  
**Fix:** Keep the `ANALYTICS_PATIENTS` names in sync with the seed patient list. At minimum, ensure total counts match (50 patients, ~22 pre-op, ~28 post-op).

### 6. Auth file might not exist on fresh deployment
**Risk:** `auth_users.json` is gitignored (likely). The demo doctor account won't exist.  
**Fix:** `_ensure_demo_doctor()` called at startup handles this — it creates the file and account if not present.

### 7. Pre-op patients' resources format
**Risk:** Pre-op patients use `resources.preop` but the battlecard rendering in `doctor.html` might look for `resources.diagnosis`/`resources.treatment`.  
**Fix:** Verify the pre-op detail modal reads `resources.preop`. If not, ensure Tej's data also has stub `diagnosis`/`treatment` entries OR confirm the frontend handles pre-op correctly.

### 8. SQLite `survey_responses` has UNIQUE(patient_id, survey_day)
**Risk:** If seed runs twice, the second `save_survey_response` for Thenuk will fail with integrity error.  
**Fix:** Use `INSERT OR IGNORE` or the idempotency check at the top of the SQLite seed block.

### 9. PCP referral form content
**Risk:** When a user clicks "Open Referral Form" for a sent patient, the modal still shows "Pending" for PCP Name.  
**Fix:** Pre-populate `pcpDrafts` in the frontend JS with some demo doctor names for the "sent" patients. This can be done by modifying the `renderCompliance()` function to read `p.pcpDoctor` from the patient data (a new field), or by hardcoding a lookup table in the JS.

### 10. Onboarding modal on first load
**Risk:** If `auth_users.json` doesn't have the full doctor profile (name, type, affiliations), the frontend will show the onboarding modal instead of the dashboard.  
**Fix:** `_ensure_demo_doctor()` must also set the profile fields (via the in-memory `_users` dict in auth.py) so `GET /api/doctor/profile` returns a complete profile.

---

## Verification Steps

1. Start backend: `cd backend && uvicorn main:app --reload`
2. Verify demo doctor can log in at `/` with `manan.vyas@cedarssinai.com` / `ArchangelDemo2024!`
3. Roster shows 50 patients
4. Click **Tej Patel** → should open pre-op detail with populated prep resources
5. Click **Arya Bhatia** → timeline modal shows "Day 4"; has post-discharge resources
6. Click **Thenuk Rodrigo** → timeline modal shows Day 14 survey marker (clickable) → survey modal shows score=80, tier=green, 5 answers
7. Click **Escalations** tab → shows ~20 escalations with mix of tiers; ~10 resolved
8. Click **Analytics** tab → shows realistic stats: 50 total patients, charts populated
9. Click **Compliance** tab → ~75% of patients show "Y" for PCP referral sent
10. Escalation "View Conversation" → opens chat snapshot modal with sample dialogue
