# PRD — TEAM Pre-Op Intake, Triage Tracking & Embedded Telehealth (Archangel Health / CareGuide)

| Field | Value |
|---|---|
| Document version | 0.2 (rewritten to match current codebase) |
| Owner | Tej Patel |
| Status | Build-ready prototype spec |
| Last updated | 2026-05-01 |
| Target users | Acute care hospitals participating in CMS TEAM Model (PY1+) |
| Implementation scope | Extend the existing CareGuide stack (FastAPI + static HTML/JS frontend + Vite/React landing app) — NOT a greenfield Next.js project |
| Audience | Cursor / Claude Code (primary), product reviewers (secondary) |

> This PRD is a v0.2 rewrite of v0.1. v0.1 assumed a greenfield Next.js + Prisma + Postgres app. The actual repo (`tej-hippocampi/archangel-health`) is a single FastAPI service plus a separate Vite/React landing page, with SQLite (`team.db`) and an in-memory patient store. All features below are specified against the **real** stack and reuse the modules that already ship.

---

## 0. What already exists in the repo (read this first)

Before touching anything, know what is already wired up.

### Backend — `backend/` (Python 3, FastAPI, SQLite)

| Module | What it does today |
|---|---|
| `main.py` (~4,000 LOC) | FastAPI app. Mounts `frontend/` at `/static`, serves doctor portal at `/`, patient dashboard at `/patient/{id}`, pre-op page at `/patient/{id}/pre-op`, voice/avatar at `/patient/{id}/digital-care-companion`. All major routes live here. |
| `team_store.py` | SQLite store. Tables: `episodes`, `event_logs`, `survey_sends`, `survey_responses`, `escalations`, `daily_reminders`, `preop_intake_submissions`, plus tenant/auth tables. Use this — do NOT add Prisma/Postgres. |
| `preop_survey.py` | T-96 / T-48 / T-24 timed pre-op survey banks; scoring; tier mapping (`green` / `orange` / `red`); window/hours logic relative to surgery datetime. |
| `intake_form_parser.py` | 11-section pre-op intake schema (`section1_demographics`…`section11_*`), red-flag detection, AI patch merging. |
| `intake_section_chat.py` | Section-scoped Claude interview turn (`run_intake_section_turn`) plus reference conversation samples in `intake_section_prompts/`. |
| `intake_form_library.json`, `intake_frameworks.json` | Specialty-keyed form library + framework definitions (loaded lazily by `main.py`). |
| `auth.py`, `tenant_jwt.py`, `tenant_utils.py`, `staff_context.py`, `tenant_constants.py` | Landing JWT auth and per-tenant (slug) auth layer. |
| `routers/admin.py`, `routers/internal.py`, `routers/onboarding.py`, `routers/tenant_portal.py` | Admin portal, prompt lab, health-system onboarding wizard, tenant sign-in. |
| `pipeline/` (`ingest`, `classify`, `extract`, `generate`) | EHR PDF → structured data → battlecard/voice script generation. |
| `prompts/` | Claude prompt templates: `preop`, `postop`, `diagnosis`, `treatment`, `avatar`, `registry`. |
| `integrations/tavus.py` | Tavus AI conversational video avatar — used today for the "talk to your care companion" experience. |
| `integrations/elevenlabs.py` | TTS for voice script. |
| `integrations/twilio_client.py` | SMS. |
| `email_utils.py`, `onboarding_emails.py` | SendGrid + SMTP fallback. |
| `tests/test_preop_survey.py` | Only existing test. |

Existing endpoints relevant to this PRD (non-exhaustive — see `backend/main.py`):

- `POST /api/intake-forms/start-interview`, `POST /api/intake-forms/{id}/interview/section-message`, `POST .../complete-section`, `POST .../reset-section`, `POST .../complete-interview`
- `GET /api/intake-forms/{id}`, `GET /api/intake-forms/latest/{patient_id}`, `PATCH /api/intake-forms/{id}`, `POST /api/intake-forms/{id}/submit`, `GET .../edit-history`
- `POST /api/pre-op/intake/start | answer | submit`, `GET /api/doctor/patient/{patient_id}/latest-intake`, `POST /api/pre-op/notify-care-team`
- `GET /api/preop-survey/questions`, `POST /api/preop-survey/submit`, `GET /api/patients/{id}/preop-window/{window}`, `POST .../action`
- `POST /api/survey/submit` (post-op daily survey), `GET /api/escalations`, `PATCH /api/escalations/{id}/resolved`, `POST /api/escalations/consent`
- `POST /api/process-preop`, `POST /api/process-discharge`, `POST /api/process-patient`, `POST /api/upload-pdf`, `POST /api/send-to-patient/{id}`
- `POST /api/digital-care-companion/chat` and alias `/api/avatar/chat`

Existing escalation tier model (`team_store.escalations.tier`):

- **Tier 1** — self-care guidance, no human escalation (hard-tier-1 phrases).
- **Tier 2** — same-day surgeon contact (semantic).
- **Tier 3** — navigator follow-up within 24h (semantic).

This is not the same axis as the PRD's pre-op risk tiers and must be reconciled (see §9).

### Static frontend — `frontend/` (no build step)

`index.html` (post-op patient dashboard), `pre-op.html` + `pre-op.js`, `preop-survey.html` + `preop-survey.js`, `doctor.html` (doctor portal), `voice-avatar.html`, `upload.html` (PDF upload), `admin.html`, `prompt-lab.html`, `app.js`, `styles.css`. FastAPI serves these via `/static`.

### Landing app — `landing/` (Vite + React 18 + Tailwind v4 + Radix shadcn + MUI)

Marketing site, sign-in/sign-up, health-system onboarding wizard, TEAM calculator, white paper. Lives at `landing/src/app/App.tsx` with components under `landing/src/app/components/`. Uses `react-hook-form`, `recharts`, `react-router` v7. Proxies `/api/*` to backend in dev. Deployed separately (Vercel target).

### Stack constraints (what NOT to add)

- **Do not introduce** Next.js, Prisma, NextAuth/Auth.js, Drizzle, or Postgres. Storage = SQLite via `team_store.py` (+ in-memory patient roster in `main.py`). Auth = `auth.py` (python-jose JWT) and `tenant_jwt.py`.
- **Do not introduce** Daily.co/Twilio Video/Zoom in v0.1. The repo already integrates **Tavus**, which is the default video provider for telehealth visits in this PRD. If we later need a clinician↔patient (two-human) video call, we'll add Daily.co behind the same `VideoProvider` interface — out of scope for v0.1.
- **Do not introduce** a separate React SPA for clinical workflows. Doctor / patient / admin / pre-op pages stay as static HTML in `frontend/` served by FastAPI. Any net-new clinical UI in this PRD ships as new HTML/JS in `frontend/`. The Vite/React app is reserved for marketing + auth + onboarding.
- **Frameworks/UI**: vanilla HTML + the existing `frontend/styles.css` for clinical pages; `react-hook-form` + Radix shadcn (already installed) for landing/onboarding additions only.

---

## 1. Context & Problem

CMS's Transforming Episode Accountability Model (TEAM) launched January 1, 2026. Effective April 6, 2026, the TEAM telehealth waiver introduces nine new HCPCS codes (G0660–G0668) and a strict billing rule set (Demo A9, TOB 13X, Rev 0780, POS 02/10, ride-alone claims).

The financial structure inverts FFS incentives: every telehealth visit both generates revenue *and* counts against the episode target price. This product is the operational layer that captures intake, scores activation, verifies eligibility, drives triage, and produces clean TEAM telehealth claims.

## 2. Goals & Non-Goals

### Goals (this release)

- Capture every TEAM-relevant pre-op data point in a single ≤15-minute intake session, reusing the existing 11-section schema and section-by-section AI interview.
- Verify TEAM eligibility from any of three artifact formats (X12 271, PDF, CSV) with explainable, auditable results.
- Produce a baseline **risk tier** for every enrolled patient before surgery (new axis, distinct from the existing escalation tier).
- Re-tier patients dynamically through the 30-day episode based on post-op signals, reusing the existing `escalations` + post-op survey infrastructure.
- Surface escalation candidates to an RN coordinator with full evidence packets.
- Conduct compliant TEAM telehealth visits inside the product via the existing **Tavus**-based video surface, with claim attributes auto-populated.
- Achieve >95% first-pass clean-claim rate on generated TEAM telehealth claims.

### Non-Goals (deferred)

- Native EHR write-back (we read EHR via PDF/FHIR; we don't push notes back).
- Pre-hab referral routing automation.
- Surgeon performance/comp dashboards.
- Episode-level financial reconciliation modeling.
- Production-grade two-party WebRTC visits (Daily.co/Twilio Video). v0.1 uses Tavus.
- Patient mobile native app (responsive web only).
- Multi-tenant administration UI beyond what `routers/admin.py` and `routers/onboarding.py` already provide.
- Postgres / Prisma migration. v0.1 stays on SQLite (`team.db`).

## 3. Personas

| Persona | Touchpoints in this release |
|---|---|
| Pre-op nurse coordinator | Intake interview (`/patient/{id}/pre-op`), eligibility checker (new), baseline risk tier on episode |
| RN triage coordinator | Triage queue (new page in `frontend/`), escalation actions, tier overrides |
| NP / PA (post-op telehealth) | Telehealth visit room (Tavus session + clinician sidebar), in-call documentation, close-out |
| Surgeon | Doctor portal (`/`), escalation pings (existing `escalations` tab) |
| Patient | Patient dashboard (`/patient/{id}`), pre-op page, daily survey, Tavus visit join link |
| Billing / RCM specialist | Claim review queue (new page in `frontend/`), claim audit trail |

## 4. System overview

```
┌────────────────────────────── Pre-op Intake (Feature 1) ──────────────────────────────┐
│  Reuses /api/intake-forms/* + /api/pre-op/intake/*                                    │
│  ┌─────────────────────────────┐  ┌──────────────────────────────────────────────┐   │
│  │  Activation proxy (Feature 2)│  │  TEAM eligibility checker (Feature 3 — NEW)  │   │
│  │  New 13Q section in intake  │  │  X12 271 / PDF / CSV → 6 checks               │   │
│  └─────────────────────────────┘  └──────────────────────────────────────────────┘   │
└──────────────┬──────────────────────────────────────────┬────────────────────────────┘
               │ activation tier                          │ eligibility verdict
               ▼                                          ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │   Triage Tracking (Feature 4 — extends existing escalations)    │
   │   New `episodes.risk_tier` (TIER_1/2/3) + dynamic re-tiering    │
   │   → RN queue (new HTML page) → existing escalations table       │
   └────────┬───────────────────────────────────────────┬────────────┘
            │ "telehealth needed"                       │ scheduled cadence
            ▼                                           ▼
   ┌────────────────────────────────────────────────────────────────┐
   │   Telehealth Video (Feature 5 — wraps existing Tavus)          │
   │   VideoProvider interface (Tavus today, Daily.co later)        │
   │   → in-call → close-out → auto-built TEAM claim                │
   └────────────────────────────────────────────────────────────────┘
```

## 5. Tech stack & assumptions (matches the repo)

- **Backend**: Python 3, FastAPI 0.115, uvicorn, pydantic v2.
- **Storage**: SQLite via `team_store.py` (path `TEAM_DB_PATH`, default `backend/team.db`); in-memory patient store with optional JSON persistence (`DEMO_PERSIST_PATIENT_STORE`, `DEMO_PATIENT_STORE_PATH`).
- **Auth**: `auth.py` (python-jose JWT, bcrypt via passlib), tenant JWT in `tenant_jwt.py`.
- **AI**: `anthropic==0.34.0` — Claude Sonnet for intake interview, eligibility extraction, semantic escalation. Use `tool_use` with structured input schemas for extraction tasks.
- **PDF**: `PyPDF2==3.0.1` (already in `requirements.txt`). For OCR fallback on scanned PDFs, add `pytesseract` + `Pillow` and require system `tesseract-ocr` (Dockerfile update).
- **CSV**: standard library `csv` plus a fuzzy header matcher (no `pandas`).
- **X12 271**: hand-rolled segment parser (no good Python lib). Place under `backend/eligibility/x12.py`.
- **Video**: `integrations/tavus.py` already wired. Wrap behind `backend/video/provider.py` `VideoProvider` interface. Default impl `TavusProvider`. Stub `DailyProvider` for future swap.
- **Storage for uploads**: filesystem under `/var/data/eligibility/{episode_id}/{sha256}.{ext}` for prototype (no S3 dependency yet); add S3 behind a simple `Storage` interface when we go to production.
- **SMS / email**: existing Twilio + SendGrid wiring.
- **Frontend (clinical)**: vanilla HTML + JS in `frontend/` reusing `styles.css`.
- **Landing/onboarding**: Vite/React additions only when the surface is marketing or auth.

### Compliance assumptions

- BAAs required pre-prod: Anthropic, Tavus, Twilio, SendGrid, hosting (Railway), and any future Daily.co.
- PHI encrypted at rest (filesystem + SQLite encryption at the volume level for production; AES-256 on uploads). TLS 1.3 in transit.
- Every billing-relevant or data-altering action writes to a new `audit_log` table in SQLite (added in this release; see §11.1).
- Session timeout: enforce 15-minute inactivity for clinician roles in `auth.py` (today JWT TTL is generous — tighten for clinician scopes).
- Not a medical device. AI outputs are decision support; clinician confirmation required.

---

## 6. Feature 1 — Pre-Op Intake (extend existing flow)

### 6.1 What's already there

- `intake_form_parser._schema()` defines an 11-section intake (demographics, surgical info, medical history, surgical/anesthesia history, meds/allergies, social, family, ROS, functional, day-of-readiness, plus consent/notes).
- `intake_section_chat.run_intake_section_turn` runs Claude per-section using sample-conversation reference files.
- `team_store.preop_intake_submissions` persists submissions; `intake_form_id` flows through `_create_intake_notifications` to alert doctors.
- The patient-facing page is `frontend/pre-op.html` + `pre-op.js`.

### 6.2 What this feature adds

1. A new **Episode** record (NEW table — see §6.5) created when a coordinator finalizes intake for a TEAM-eligible patient. The intake submission is linked to the episode.
2. Two new sections grafted into the intake schema:
   - `section12_teamEligibility` (drives Feature 3)
   - `section13_activation` (drives Feature 2 — 13-question PAM-style proxy)
3. A **risk-tier preview** screen (read-only) shown before finalize.
4. Coordinator override of risk tier with required reason → audit log.
5. Finalize blocker logic: episode cannot be finalized as TEAM if eligibility verdict is `INELIGIBLE`.

### 6.3 User stories

- US-1.1 As a pre-op nurse coordinator, I select the TEAM anchor procedure (LEJR / hip-femur fracture / spinal fusion / CABG / major bowel) when starting an episode.
- US-1.2 The intake auto-saves per section (already true today via `complete-section`).
- US-1.3 A progress indicator shows section status (already in `pre-op.js` — confirm coverage of new sections 12/13).
- US-1.4 At the end I see a "ready for surgery" verdict with blockers itemized.
- US-1.5 I can resume from any device (already supported via `intake-forms/latest/{patient_id}`).

### 6.4 Acceptance criteria

- AC-1.1 GIVEN a finalized intake WHEN any required field across sections 1–13 is missing THEN the "Finalize episode" button is disabled and missing fields are listed with deep links into the section.
- AC-1.2 In-progress intake resumes from last completed section within 30 days (use the existing `intake-forms/latest/{patient_id}`).
- AC-1.3 Eligibility verdict `INELIGIBLE` blocks "Finalize as TEAM episode"; a "Save as standard episode" option remains available (creates an episode with `program = STANDARD`).
- AC-1.4 Activation tier `LOW` pre-checks the "social work" prehab flag.
- AC-1.5 Trained coordinator completes intake → finalize in ≤15 min (telemetry: `intake_finalized` event with `duration_ms`).

### 6.5 Data model additions (SQLite, in `team_store.py`)

```sql
CREATE TABLE IF NOT EXISTS episodes_team (
  id              TEXT PRIMARY KEY,         -- uuid
  patient_id      TEXT NOT NULL,
  program         TEXT NOT NULL,            -- 'TEAM' | 'STANDARD'
  anchor_procedure TEXT NOT NULL,           -- 'LEJR' | 'HIP_FEMUR' | 'SPINAL_FUSION' | 'CABG' | 'MAJOR_BOWEL'
  surgeon_id      TEXT,
  hospital_id     TEXT,
  surgery_date    TEXT NOT NULL,            -- ISO date
  episode_start   TEXT,                     -- set on discharge
  episode_end     TEXT,                     -- surgery_date + 30d
  intake_form_id  TEXT,
  eligibility_id  TEXT,
  activation_id   TEXT,
  risk_tier       TEXT,                     -- 'TIER_1' | 'TIER_2' | 'TIER_3'
  risk_tier_source TEXT,                    -- 'SYSTEM' | 'OVERRIDE'
  risk_tier_reason TEXT,
  status          TEXT NOT NULL,            -- 'DRAFT' | 'ACTIVE' | 'CLOSED' | 'CANCELLED'
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

> Note the existing `episodes` table in `team_store.py` is keyed by `patient_id` and tracks the postop 30-day window. Keep it intact; add `episodes_team` alongside (or alter to add fields). Final decision: add a new table to avoid breaking the existing daily-jobs scheduler.

### 6.6 API contracts (new)

- `POST /api/team/episodes` — create draft from a `patient_id` + anchor procedure.
- `PATCH /api/team/episodes/{id}` — update draft fields.
- `POST /api/team/episodes/{id}/finalize` — atomic finalize; returns 422 with blocker list if not ready.
- `GET /api/team/episodes/{id}` — full episode + linked intake + eligibility + activation + risk tier.
- `GET /api/team/episodes?status=ACTIVE&surgeonId=...` — list for queue.

### 6.7 Edge cases

- Coordinator picked TEAM by mistake → "Convert to standard episode" sets `program = STANDARD`, intake stays attached, eligibility / activation become optional.
- Overlapping TEAM episode in last 30 days → blocker with explanation.
- Surgery rescheduled → `surgery_date` editable until finalize; recompute `episode_end`.

---

## 7. Feature 2 — Patient Activation Assessment (PAM-style proxy)

### 7.1 Goal & licensing note

Capture a baseline activation score that feeds the risk tier. PAM-13 is proprietary. We ship an **original 13-question proxy** behind an `ActivationInstrument` interface so a licensed PAM-13 can swap in.

### 7.2 What's there to reuse

- `preop_survey.py` already implements Likert + Yes/No scoring, `_mean`, tier mapping, and JSON banks. Mirror its style for the activation instrument.
- `team_store.survey_responses` already supports a `survey_type` discriminator (`'postop'` default). Add a new survey_type `'activation_proxy_v1'` rather than a new table.

### 7.3 The 13 questions (proxy instrument)

Each on a 5-point scale: 1 (Strongly disagree) → 5 (Strongly agree), plus N/A.

1. I believe I am the most important person in my own care team.
2. I'm confident I can follow through on the medical treatments I need to do at home.
3. I know what each of my prescribed medications is for.
4. I'm confident I can tell whether I need to call my doctor or go to the ER.
5. I know how to prevent further problems with my health condition.
6. I'm confident I can keep my health problems from interfering with my daily life.
7. I know what to do if my symptoms get worse.
8. I'm confident I can talk with my care team about anything that concerns me.
9. I'm confident I can follow through on changes to my diet or exercise even when stressed.
10. I know how to prevent infection at my surgical site.
11. I'm confident I can manage my pain at home with the plan we've made.
12. I have someone at home or nearby who can help me through recovery.
13. I'm confident I can stick with my recovery plan even on hard days.

### 7.4 Scoring (Python, in a new `backend/activation.py`)

```python
def compute_activation_score(responses: dict[str, int | str]) -> dict:
    valid = [v for v in responses.values() if isinstance(v, int) and 1 <= v <= 5]
    if len(valid) < 8:
        return {"raw_score": 0, "tier": "LOW", "confidence": "LOW"}
    mean = sum(valid) / len(valid)             # 1..5
    raw_score = ((mean - 1) / 4) * 100          # 0..100
    tier = "LOW" if raw_score < 47 else "MODERATE" if raw_score < 67 else "HIGH"
    confidence = "HIGH" if len(valid) >= 12 else "MEDIUM" if len(valid) >= 10 else "LOW"
    return {"raw_score": raw_score, "tier": tier, "confidence": confidence}
```

Thresholds (47/67) live in a `backend/tuning.json` alongside any other tunables.

### 7.5 Pluggable instrument interface (Python)

```python
class ActivationInstrument(Protocol):
    id: str
    version: str
    questions: list[dict]
    def score(self, responses: dict) -> dict: ...

PROXY_V1 = ActivationInstrumentImpl(...)   # this PRD's 13Qs
# Future: PAM_13 = adapt_licensed_pam(...)
```

### 7.6 Acceptance criteria

- AC-2.1 13Qs answered → tier in <100 ms (pure Python).
- AC-2.2 >5 N/A → "Insufficient data — re-administer or treat as Low"; confidence `LOW`.
- AC-2.3 Submitted scores immutable. Re-administrations create new `survey_responses` rows with the same `patient_id` + new `submitted_at`.

### 7.7 Storage

Persist via `team_store.record_survey_response(patient_id, survey_type='activation_proxy_v1', survey_day=<negative offset from surgery>, answers=…, score=…, tier=…)`. The episode keeps a pointer (`episodes_team.activation_id`) to the latest pre-op administration.

---

## 8. Feature 3 — TEAM Eligibility Checker (NEW module)

### 8.1 Goal

Given X12 271, PDF, or CSV input, determine with citations whether the patient meets all six TEAM eligibility criteria.

### 8.2 The six checks

| # | Check | Pass condition |
|---|---|---|
| 1 | Part A active | Coverage active on surgery date |
| 2 | Part B active | Coverage active on surgery date |
| 3 | Original Medicare (not MA) | No Part C enrollment on surgery date |
| 4 | Medicare primary | No MSP indicating Medicare secondary |
| 5 | Not ESRD-basis | Eligibility basis is age or disability, not ESRD |
| 6 | Not UMWA | Not enrolled in UMWA Health Plan |

### 8.3 Architecture (new `backend/eligibility/`)

```
backend/eligibility/
  __init__.py
  router.py        # FastAPI APIRouter mounted at /api/eligibility
  ingest.py        # multipart upload → format detection → dispatch
  x12.py           # ISA/GS/ST/EB/MSG/DTP/REF/III/AAA segment parser
  pdf.py           # PyPDF2 text + pytesseract OCR fallback
  csv_parser.py    # csv + fuzzy header matcher (rapidfuzz optional)
  extract.py       # Claude tool_use structured extraction
  evaluate.py      # deterministic verdict from extracted fields
  schemas.py       # pydantic models
  storage.py       # filesystem upload storage + sha256
```

`/api/eligibility/upload` accepts one or more files (`.x12`, `.271`, `.txt`, `.pdf`, `.csv`), persists raw bytes under `/var/data/eligibility/{episode_id}/{sha256}.{ext}`, returns an `EligibilityCheck.id`. `/api/eligibility/{id}` returns parsed/extracted/verdicts. `/api/eligibility/{id}/override` records a coordinator override with required reason.

### 8.4 X12 271 parser

Parse top-down: ISA → GS → ST*271 → BHT → 2000A loops → 2100A subscriber → 2110C/D benefit info. Key signal segments:

- **EB**: EB01 (`1`=Active, `6`=Inactive, `V`=Cannot Process); EB03 (`30`=Health, `MA`=Part A, `MB`=Part B); EB04 (`MA`/`MB`/`MC`/`HM`).
- **MSG**: free text — scan for `ESRD`, `MSP`, `MEDICARE ADVANTAGE`, `UMWA`.
- **DTP**: `346`=Plan Begin, `347`=Plan End.
- **REF**: payer-specific IDs; MA contract IDs follow `H####`/`R####`/`S####`.
- **III**: industry codes for MSP detection.
- **AAA**: "Unable to Respond" → surface as parse error, NOT eligibility failure.

Output: pydantic `X12_271` with `subscriber`, `benefits[]`, `msp`, `globalMessages`, `raw`, plus `rawSegments` per benefit for citation.

### 8.5 PDF and CSV

- **PDF**: layer 1 `PyPDF2` text extraction; layer 2 if <50 chars/page → `pytesseract` OCR. Add `tesseract-ocr` to the Dockerfile.
- **CSV**: stdlib `csv` with fuzzy header matching (manual Levenshtein or `rapidfuzz`) against canonical names (`partA_eff`, `Part A Eff Date`, `MEDICARE_A_START`, …). If max match score <0.8, fall through to LLM with whole CSV as context.

### 8.6 LLM extraction (Claude `tool_use`)

Tool name `extract_team_eligibility`, input schema same as v0.1 §8.6 (Part A/B status + dates + sourceExcerpt; MA enrolled + contractId; MSP isPrimary; ESRD basis; UMWA; overallConfidence). Use Anthropic Python SDK with `tools=[…]` and `tool_choice={"type": "tool", "name": "extract_team_eligibility"}`. Pass the format-specific normalized text as the user message; the system prompt mirrors v0.1 §8.6.

### 8.7 Deterministic verdict

```python
def evaluate(extracted, surgery_date) -> dict[str, str]:
    return {
      "partAActive":     pass_(extracted.partA.status == "ACTIVE" and active_on(extracted.partA, surgery_date)),
      "partBActive":     pass_(extracted.partB.status == "ACTIVE" and active_on(extracted.partB, surgery_date)),
      "notMA":           pass_(extracted.medicareAdvantage.enrolled == "NO"),
      "medicarePrimary": pass_(extracted.medicarePrimary.isPrimary == "YES"),
      "notESRDBasis":    pass_(extracted.esrdBasis.isESRDBasis == "NO"),
      "notUMWA":         pass_(extracted.umwa.isUMWA == "NO"),
    }
```

`pass_` returns `'PASS' | 'FAIL' | 'UNKNOWN'`. Episode is TEAM-eligible iff all six PASS. Any FAIL → INELIGIBLE. Any UNKNOWN → BLOCKED_UNKNOWN until override or fresh document.

### 8.8 UI (new `frontend/eligibility.html` embedded as iframe in `pre-op.html`)

- Drag-and-drop zone with file-type detection.
- Multi-file upload.
- Per-check display: green check / red X / amber question + "Show source" disclosure highlighting the source excerpt.
- Override button → modal with required reason → POST `/api/eligibility/{id}/override`.
- "Re-run extraction" button (no re-upload).

### 8.9 Acceptance criteria

- AC-3.1 Valid X12 271 → all six checks resolve in <15s.
- AC-3.2 Scanned PDF (OCR fallback) → <60s.
- AC-3.3 CSV with non-standard headers (max fuzzy <0.8) → falls through to LLM with notice.
- AC-3.4 Override writes `audit_log` row `eligibility_override` with actor/timestamp/reason.
- AC-3.5 ESRD ambiguity (basis vs. comorbidity) → check returns UNKNOWN, never silently PASS.
- AC-3.6 ≥95% first-pass accuracy on a 50-document labeled validation set checked into `backend/eligibility/fixtures/`.

### 8.10 Data model (SQLite)

```sql
CREATE TABLE IF NOT EXISTS eligibility_checks (
  id              TEXT PRIMARY KEY,
  episode_id      TEXT NOT NULL,
  uploaded_files  TEXT NOT NULL,        -- json: [{filename, format, path, sha256}]
  parsed_docs     TEXT NOT NULL,        -- json
  extracted       TEXT NOT NULL,        -- json
  verdicts        TEXT NOT NULL,        -- json
  overall_verdict TEXT NOT NULL,        -- 'ELIGIBLE' | 'INELIGIBLE' | 'BLOCKED_UNKNOWN'
  overrides       TEXT,                 -- json array
  llm_model       TEXT NOT NULL,        -- e.g. 'claude-sonnet-4-6'
  llm_request_id  TEXT,
  created_at      TEXT NOT NULL,
  created_by      TEXT NOT NULL
);
```

### 8.11 Edge cases

- AAA "Unable to Respond" → parse error, not eligibility failure.
- Password-protected PDF → block with helpful message.
- Eligibility older than surgery date by >7 days → freshness banner; offer re-run.
- Conflicting documents → highlight conflicts, ask coordinator to choose authoritative source.

---

## 9. Feature 4 — Triage Tracking

### 9.1 Goal

Maintain a continuously-updated **risk tier** (TIER_1 / TIER_2 / TIER_3) per active TEAM episode, drive surveillance cadence, and surface escalations to the RN coordinator with full context.

### 9.2 Naming reconciliation (important)

The repo already has an "escalation tier" axis (1/2/3) on the `escalations` table — that means *severity of an individual symptom event*. The new **risk tier** in this PRD means *the patient's standing tier across the episode*. To avoid name collisions:

- Existing column stays: `escalations.tier` = severity of individual event (1=self-care, 2=same-day surgeon, 3=navigator).
- New column: `episodes_team.risk_tier` ∈ {`TIER_1` Standard, `TIER_2` Enhanced, `TIER_3` High-Touch}.

UI must visually distinguish the two ("Risk tier: Enhanced" vs "Escalation: Tier 2").

### 9.3 The three risk tiers

| Risk tier | Profile | Default cadence | Threshold posture |
|---|---|---|---|
| TIER_1 Standard | Low pre-op risk, high activation, supported home | Standard 4-touchpoint cadence (D2-3, D7-10, D21-25, D28-29) | Default thresholds |
| TIER_2 Enhanced | Moderate risk: any of {moderate activation, comorbidity burden, transportation barrier, age >75 with single comorbidity} | Standard + 1 add'l touchpoint at D14 | Tightened ~15% (e.g., temp 100.0°F vs 100.4°F) |
| TIER_3 High-Touch | High risk: any of {Low activation, ≥3 comorbidities or Charlson ≥3, prior 30-d readmission, lives alone with no caregiver, low housing/food security} | Daily RN check-in for first 7 days, then every 2-3 days through D28 | Lowest thresholds; daily RN review regardless of alerts |

### 9.4 Initial tier assignment (Python, in `backend/triage.py`)

```python
def assign_initial_tier(episode, intake, activation) -> str:
    r = compute_risk_factors(episode, intake, activation)
    if r.activation_tier == "LOW": return "TIER_3"
    if r.prior_readmission_within_30d: return "TIER_3"
    if r.charlson_index >= 3: return "TIER_3"
    if r.lives_alone and not r.has_reliable_caregiver: return "TIER_3"
    if r.housing_insecure or r.food_insecure: return "TIER_3"
    score = sum([
      r.activation_tier == "MODERATE",
      r.charlson_index >= 1,
      r.transportation_barrier,
      r.age >= 75,
      r.active_smoker,
    ])
    return "TIER_2" if score >= 2 else "TIER_1"
```

Signals come from the existing intake schema (sections 3, 6, 9, 10) plus the new activation section (13). Computed at finalize; persisted on `episodes_team.risk_tier`. Coordinator override writes `risk_tier_source = 'OVERRIDE'` + `risk_tier_reason`.

### 9.5 Dynamic re-tiering during the episode

Trigger nightly (extend `internal/team/run-daily-jobs`) and on signal events. Upgrades automatic; downgrades require RN confirmation.

```python
def reevaluate_tier(current, signals) -> str:
    if signals.patient_self_flagged: return "TIER_3"
    if signals.wound_concern_high:   return "TIER_3"
    if signals.temp_sustained_above(100.4): return upgrade(current)
    if signals.pain_trajectory_abnormal:    return upgrade(current)
    if signals.missed_readings_gt_24h and current == "TIER_3": return "TIER_3"
    if signals.missed_readings_gt_48h:      return upgrade(current)
    return current
```

Inputs come from existing post-op survey responses, post-op events (`event_logs`), and new RPM webhook ingestion (out of scope for v0.1 to *integrate device APIs*; in scope to *consume* a generic `POST /api/rpm/signal` payload).

### 9.6 RN triage queue (the load-bearing screen)

Implement as a new HTML page `frontend/triage.html` + `frontend/triage.js`, served at `/triage` (auth-gated to RN/clinician roles). Re-uses `frontend/styles.css`.

- **Top bar** filters: risk tier, anchor procedure, days post-op, assigned RN, full-text search.
- **Left**: patient list, default-sorted by `priority_score DESC`.
- **Right (selected patient)** 3-column expandable detail:
  - A: patient summary + day-of-episode + current risk tier (override badge if set).
  - B: signals & evidence — RPM trend sparkline (recharts? — use lightweight inline SVG to avoid React in `frontend/`), last symptom check-in, wound photo carousel, missed readings, recent alerts list.
  - C: actions — Phone call (logs duration), Schedule NP/PA telehealth (opens visit scheduler), Escalate to surgeon (creates `escalations` row with `tier=2`), Mark resolved (PATCH `/api/escalations/{id}/resolved`), Adjust risk tier (with reason).
- Min 8 patients visible without scroll on a 1440px display. Card shows: name, "Day 4 of 30", risk-tier badge, top 2 alert reasons.

### 9.7 Priority score

```python
def priority_score(s, episode) -> float:
    score = 0
    if s.patient_self_flagged: score += 100
    if s.wound_concern_high:   score += 80
    if s.temp_sustained_above(100.4): score += 60
    if s.pain_trajectory_abnormal:    score += 40
    if s.missed_readings_gt_48h: score += 30
    if s.missed_readings_gt_24h: score += 15
    if episode.risk_tier == "TIER_3": score += 20
    if episode.risk_tier == "TIER_2": score += 10
    return apply_time_decay(score, s.oldest_unreviewed_alert_at)
```

Weights tunable in `backend/tuning.json`.

### 9.8 Acceptance criteria

- AC-4.1 Threshold breach → queue updates within 30s (poll every 15s; SSE later).
- AC-4.2 No RN action within 4h business hours → red banner + Twilio SMS to on-call RN (use `integrations/twilio_client.py`).
- AC-4.3 Patient self-flag → immediate confirmation message + ≤15 min business / ≤1h after-hours promise.
- AC-4.4 Tier changes write to `audit_log` (actor, prior, new, factors).
- AC-4.5 Weekly QA: ≥97% queue alerts result in a documented action; <3% silent dismissals.

### 9.9 Data model

```sql
CREATE TABLE IF NOT EXISTS triage_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id   TEXT NOT NULL,
  type         TEXT NOT NULL,    -- 'ALERT_RAISED' | 'ALERT_RESOLVED' | 'TIER_CHANGED' | 'RN_ACTION'
  signals_json TEXT,
  from_tier    TEXT,
  to_tier      TEXT,
  priority_at  REAL,
  actor        TEXT NOT NULL,    -- userId or 'SYSTEM'
  reason       TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triage_alerts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id      TEXT NOT NULL,
  reason          TEXT NOT NULL, -- 'WOUND_CONCERN_HIGH' | 'TEMP_SUSTAINED' | …
  evidence_json   TEXT,
  status          TEXT NOT NULL, -- 'OPEN' | 'IN_REVIEW' | 'RESOLVED' | 'ESCALATED'
  resolved_by     TEXT,
  resolution_note TEXT,
  created_at      TEXT NOT NULL,
  resolved_at     TEXT
);
```

### 9.10 Edge cases

- Patient unreachable → soft-defer with 2-hour timer.
- Admitted to another hospital → flag "Possible interrupted episode".
- Multiple concurrent alerts on same patient → collapse into single card with stacked alert pill.
- Day 31+ → `episodes_team.status = 'CLOSED'`; alerts informational only.

---

## 10. Feature 5 — Embedded Telehealth Video Conferencing

### 10.1 Goal

A video visit experience that loads in <3s, no install for the patient, captures data for a clean TEAM telehealth claim, and produces an auto-built claim at call end.

### 10.2 Vendor strategy (defaulting to Tavus, abstracting for Daily.co)

`backend/video/provider.py`:

```python
class VideoProvider(Protocol):
    async def create_session(self, *, episode_id: str, participants: list[Participant], recording_policy: RecordingPolicy) -> VideoSession: ...
    async def generate_join_token(self, session_id: str, participant: Participant) -> JoinToken: ...
    async def end_session(self, session_id: str) -> EndedSession: ...
    async def get_recording(self, session_id: str) -> Recording | None: ...
```

- `TavusProvider` (default) wraps `integrations/tavus.py`. Tavus today is an AI-avatar conversation, so for two-human visits we run it in "human-passthrough" mode where the avatar is muted and we use the Tavus session as a Daily-room shell. **If Tavus does not support unattended human↔human rooms in our plan, ship v0.1 with a Daily.co implementation under the same interface.** This is a known risk; Cursor should validate against current Tavus capability and add `DailyProvider` if needed.
- `DailyProvider` (stub for now): `@daily-co/daily-js` on the patient page; backend mints meeting tokens via Daily REST API.

### 10.3 User stories

Same as v0.1 §10.3 (US-5.1 through US-5.6). Patient join via SMS (Twilio already wired). Magic-link JWT signed with `AUTH_SECRET`, audience `video-join`.

### 10.4 Visit lifecycle

```
[Scheduled visit created] → [Patient SMS+email] → [Patient joins waiting room]
                                                          │
[Clinician notified] → [Clinician joins] → [Location prompt] → [Visit clock starts]
                                                                 │
                                                         [Live visit + in-call docs]
                                                                 │
[End call] → [Duration → G-code mapping] → [L4/L5 attestation if needed] → [Draft note] → [Auto-built claim]
```

### 10.5 Pre-call workflow

- Visit scheduling: triggered from RN triage queue or scheduled cadence engine. New endpoint `POST /api/team/encounters` with `{episode_id, scheduled_for, clinician_id, reason}`.
- Patient invitation: SMS + email at scheduling, reminder 1h before, "join now" link 5 min before. Magic link → waiting room (no auth UI; JWT bound to session).
- Tech check: 15-second mic/camera test in waiting room with "join by phone" fallback (Twilio Voice number).

### 10.6 In-call experience

- Patient view: simplified — video, mic/camera/leave, single chat box. Built as `frontend/visit-patient.html`.
- Clinician view: `frontend/visit-clinician.html` with sidebar:
  - Patient summary one-pager (reuse battlecard HTML generation from `pipeline/generate.py`).
  - Visit playbook (8-step collapsible: wound check, pain, function, vitals review, med rec, red flags, plan, document).
  - Documentation form (saves draft continuously to `telehealth_encounters.documentation_json`).
  - Wound photo capture: clinician asks patient to show wound; "Capture" button takes still from video stream and POSTs to `/api/team/encounters/{id}/photos`.
- Visit timer: clinician-only. Starts when both connected, pauses on drop, resumes on rejoin. Drives the G-code mapping at end-of-call.

### 10.7 G-code mapping (deterministic)

| Code | Patient type | Time threshold |
|---|---|---|
| G0660 | New | ≥10 min |
| G0661 | New | ≥20 min |
| G0662 | New | ≥30 min |
| G0663 | New (L4) | ≥45 min |
| G0664 | New (L5) | ≥60 min |
| G0665 | Established | ≥10 min |
| G0666 | Established | ≥15 min |
| G0667 | Established | ≥25 min |
| G0668 | Established (L5) | ≥40 min |

UI shows the ladder but does NOT nudge upward — under TEAM, shorter is often better. New vs. established is computed automatically from the EHR record of the rendering provider/specialty/group within 3 years; clinicians do not pick this.

### 10.8 Level 4/5 documentation gate

When visit ends and matched code ∈ {G0663, G0664, G0668}, hard modal requires one of:

1. ☐ Licensed clinical staff was on-site in the patient's home (document role + location).
2. ☐ Licensed clinical staff was not required for this visit (document clinical reasoning).

Selection + free-text justification → `telehealth_encounters.l45_attestation` (json) and surfaced on claim audit trail.

### 10.9 Claim auto-build (`backend/billing/claims.py`)

On visit end + L4/L5 attestation (if needed) + clinician sign-off, build a draft claim:

```json
{
  "episode_id": "...",
  "encounter_id": "...",
  "service_date": "2026-05-04",
  "hcpcs_code": "G0666",
  "patient_type": "ESTABLISHED",
  "duration_minutes": 17,
  "pos": "10",
  "type_of_bill": "13X",
  "revenue_code": "0780",
  "demo_code": "A9",
  "ride_alone": true,
  "l45_attestation": null,
  "documentation": { "soap": "..." },
  "rendering_provider": "...",
  "billing_provider_id": "...",
  "status": "DRAFT_READY_FOR_REVIEW",
  "audit_trail": [{"action": "...", "actor": "...", "at": "..."}]
}
```

The claim builder enforces ride-alone at the schema level (any attempt to attach a non-TEAM line item raises `RIDE_ALONE_VIOLATION`).

### 10.10 Acceptance criteria

- AC-5.1 Patient join time SMS-tap → "in waiting room" <5s on modern smartphone.
- AC-5.2 17 min established → HCPCS G0666; ladder displayed.
- AC-5.3 47 min established → close-out modal forces L4/5 attestation before claim moves to billing queue.
- AC-5.4 Any FFS line-item attempt → `RIDE_ALONE_VIOLATION`; line rejected.
- AC-5.5 Parent episode `eligibility.overall_verdict = INELIGIBLE` → block visit start with explanation.
- AC-5.6 ≥95% first-pass clean-claim rate.

### 10.11 Data model (SQLite)

```sql
CREATE TABLE IF NOT EXISTS telehealth_encounters (
  id                   TEXT PRIMARY KEY,
  episode_id           TEXT NOT NULL,
  scheduled_for        TEXT NOT NULL,
  scheduled_clinician  TEXT NOT NULL,
  vendor_session_id    TEXT,
  patient_location     TEXT,             -- 'HOME' (POS_10) | 'FACILITY_OTHER' (POS_02)
  started_at           TEXT,
  ended_at             TEXT,
  duration_minutes     INTEGER,
  hcpcs_code           TEXT,
  patient_type         TEXT,             -- 'NEW' | 'ESTABLISHED'
  l45_attestation      TEXT,             -- json
  documentation_json   TEXT,
  captured_photos      TEXT,             -- json array of file paths
  status               TEXT NOT NULL,    -- 'SCHEDULED' | 'IN_PROGRESS' | 'COMPLETED' | 'CANCELLED' | 'NO_SHOW' | 'REDIRECTED_TO_ED'
  claim_id             TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  id                   TEXT PRIMARY KEY,
  encounter_id         TEXT NOT NULL UNIQUE,
  episode_id           TEXT NOT NULL,
  hcpcs_code           TEXT NOT NULL,
  pos                  TEXT NOT NULL,
  type_of_bill         TEXT NOT NULL,
  revenue_code         TEXT NOT NULL,
  demo_code            TEXT NOT NULL,
  ride_alone           INTEGER NOT NULL DEFAULT 1,
  status               TEXT NOT NULL,
  audit_trail_json     TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
```

### 10.12 Edge cases

- Drop and rejoin → timer pauses then resumes; one encounter, accurate cumulative duration.
- Patient never connects → encounter `NO_SHOW`; no claim built.
- Clinician redirects to ED → encounter `REDIRECTED_TO_ED`; no claim; RN paged.
- Eligibility re-check fails post-start → block submission, surface in billing review.

---

## 11. Cross-cutting requirements

### 11.1 Audit log (NEW)

```sql
CREATE TABLE IF NOT EXISTS audit_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  actor        TEXT NOT NULL,
  action       TEXT NOT NULL,
  resource     TEXT NOT NULL,
  resource_id  TEXT,
  before_json  TEXT,
  after_json   TEXT,
  ip           TEXT,
  occurred_at  TEXT NOT NULL
);
```

Every data-altering write in features 1–5 calls `team_store.write_audit(...)`.

### 11.2 Non-functional requirements

- P95 page load <2s (static HTML helps); queue updates <30s after signal; video join <5s.
- 99.9% target; degraded mode (read-only) when external providers down.
- WCAG 2.1 AA throughout; iOS/Android Chrome+Safari verified; keyboard-navigable; screen-reader compatible.
- English only in v0.1 (architecture should not block Spanish in v0.2).
- HIPAA: encrypt at rest (filesystem-level + AES-256 on uploads) and in transit (TLS 1.3); access logs retained 6 years; PHI never sent to non-BAA vendors.

### 11.3 Telemetry & metrics

`event_logs` is the existing event sink. Emit:

- `intake_finalized` (with duration_ms)
- `eligibility_uploaded`, `eligibility_verdict`, `eligibility_override`
- `triage_alert_raised`, `triage_alert_resolved`, `risk_tier_changed`
- `visit_started`, `visit_ended`, `claim_drafted`, `claim_submitted`

Aggregations: time-to-finalize-intake, eligibility first-pass success rate, risk-tier distribution, alerts/day, time-to-action, visit show rate, average duration by code, first-pass clean-claim rate.

### 11.4 Open questions

1. EHR integration: Epic FHIR R4 vs HL7 v2. Pilot partner identity.
2. Scheduling integration: Epic Cadence vs native scheduler.
3. Surgeon dashboard: full feature in v0.2 or stub in v0.1?
4. Tuning data: any retrospective CJR/BPCI data to calibrate thresholds before go-live?
5. RPM device strategy: device vendors supported natively in v0.1.
6. **Tavus capability**: confirm Tavus supports two-human telehealth rooms with our plan; if not, swap default to `DailyProvider` and obtain BAA.

## 12. Out of scope

Production WebRTC infra change, EHR write-back, native mobile apps, multi-tenant admin beyond what exists, surgeon comp model, RPM device hardware integration, patient billing/payment, pharmacy integration.

## 13. Glossary

TEAM, MBI, MA, MSP, ESRD, UMWA, POS, TOB, RPM, PAM, L4/L5, Ride-alone — see v0.1 §13. Unchanged.

---

*End of PRD v0.2*
