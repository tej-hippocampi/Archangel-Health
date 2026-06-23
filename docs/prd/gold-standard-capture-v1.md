# Gold Standard — Clinical Conversation Gold-Data Capture (v0.1)

**Working codename:** GoldCapture
**Surfaces as:** a new **"Gold Standard"** sub-tab inside **Population Analytics** on the **doctor portal only** (`frontend/doctor.html`).
**Owner:** Founder
**Status:** MVP / v0.1 — design-partner pilot (single surgical tenant)
**Last updated:** June 2026

> **Build instructions for Claude Code:** This is the spec for a working MVP that ships **inside the existing Archangel Health codebase** — a single-service FastAPI backend (`backend/`) serving static HTML/CSS/JS (`frontend/`). **Do not introduce Next.js, React, Supabase, Tailwind, or a build step for this feature** — the original product brief named those, but Archangel does not use them for the doctor portal. Reuse the patterns already in the repo (listed in §8). Build incrementally in the phases in §13. Where a decision isn't specified, pick the option that matches existing repo conventions and note it. Treat §10 (Compliance & Security) as non-negotiable — this app handles Protected Health Information (PHI). This PRD adapts the generic "GoldCapture" product brief to Archangel's actual stack; every place the original brief assumed a different technology, the mapping is called out in §8.

---

## 1. Summary

Gold Standard is an in-visit capture + review workflow embedded in the Archangel doctor portal. It records the surgeon–patient conversation, auto-generates a draft clinical note via the existing Anthropic LLM client, and lets the surgeon quickly **correct the draft and flag error types**. The surgeon's verified corrections become **"gold-standard" labeled training data**. Each visit is de-identified (automated + human QA) and exported as a structured JSONL record.

These records are sold (initially via the data marketplace **Protege**, and to AI medical-scribe companies) as high-value **supervised fine-tuning** and **gold-evaluation** data for clinical AI models.

**The product is the data, not the UI.** The tab exists to produce clean, clinician-verified, de-identified records as cheaply and quickly as possible, with the surgeon spending ≤45 seconds of extra effort per visit.

---

## 2. Where it lives in Archangel (placement)

- **Portal:** doctor portal only — `frontend/doctor.html`, served at `/doctor/app` after tenant JWT sign-in. **Not** the patient dashboard, **not** the admin console.
- **Navigation:** a **4th sub-tab** under Population Analytics, alongside the existing Overall / Pre-Op / Post-Op sub-tabs.
  - Markup: add `<button type="button" class="analytics-subtab-btn" data-analytics-tab="goldstandard">Gold Standard</button>` to the `.analytics-subtabs` row (`doctor.html` ~line 1705), and a sibling `<div class="analytics-subpage" id="analytics-goldstandard">…</div>` (after `#analytics-postop`, ~line 1763).
  - Wiring: register the new page in the `pages` map inside `setupAnalyticsSubtabs()` (`doctor.html` ~line 3057) — `goldstandard: byId("analytics-goldstandard")`. Lazy-load its data on first activation, mirroring how `setupTabs()` lazy-loads the compliance tab (`complianceTabLoaded` / `loadComplianceTab()`, ~line 3011).
- **Role gating (UI):** read the cached profile role (`JSON.parse(localStorage.getItem(PROFILE_KEY)).role`) exactly as `refreshIntraopReviewQueue()` does (~line 3079). Show capture + review panels to `surgeon`. Show the De-ID/QA + Export panels only when role is `system_admin` (the founder/internal operator) — but still rendered inside this same tab so the whole feature lives on the doctor portal as requested. In the single-tenant pilot, a `system_admin` who is also operating can see all panels.

> **Naming note:** the original brief is specialty-agnostic ("nephrology"). Archangel's pilot tenant is **surgical** (TEAM / ACS-NSQIP episodes). Keep `specialty` and `encounter_type` configurable (§7) so the same pipeline serves any specialty; default the pilot to the tenant's configured specialty.

---

## 3. Goals & Non-Goals

### Goals (MVP)
- Capture audio + patient consent with a single **Start Visit** action inside the Gold Standard sub-tab.
- Produce a draft clinical note from the audio automatically (STT → existing `call_llm`).
- Let the surgeon review the draft, correct it, and tag error types in ≤45 seconds.
- Capture final billing code(s) and any prior-authorization outcome.
- De-identify every record (automated + human check) before export, targeting HIPAA Safe Harbor.
- Export a schema-valid JSONL dataset + data dictionary, ready to deliver to a buyer.
- Run reliably for one tenant, ~40 visits/day.

### Non-Goals (explicitly out of scope for MVP)
- No EHR/EMR integration (Archangel's EHR ingestion pipeline is separate and **not** reused here).
- No native iOS/Android app — responsive web only; **must work in iPad Safari** (the portal already targets it; see `docs/SURGEON_UX_AUDIT.md`).
- No real-time/streaming transcription (batch after End Visit is fine; reuse the SSE progress pattern in §8).
- No payments/payouts processing (track amounts owed; pay manually).
- No new multi-tenant admin surface (reuse existing tenant scoping).
- No model training in-app (we produce data; buyers train).
- No patient-facing features beyond the in-visit consent screen.
- **No tie-in to the triage `Episode.tier` pipeline** — Gold Standard records are an independent artifact and must not write any triage field.

---

## 4. Users & Roles (mapped to Archangel roles)

| Brief persona | Archangel role (`backend/auth_roles.py`) | Sees |
|---|---|---|
| Clinician (primary) | `surgeon` | Start Visit, Consent, Review & gold-label, billing/prior-auth, "Looks correct" |
| Front-desk / MA (optional) | `rn_coordinator` | Start Visit + Consent only (capture, no gold-labeling) |
| Data/QA operator (internal — founder) | `system_admin` | De-ID QA view, approve, export |
| Admin (internal) | `system_admin` | tenant/user/API-key management (existing admin console) |

Enforce with the existing `require_roles(staff, allowed)` helper. Suggested gates:
- Capture (start/consent/end/upload): `{"surgeon", "rn_coordinator"}`
- Gold review / submit: `{"surgeon"}`
- De-ID QA, approve, export: `{"system_admin"}`

The `patient` "role" is implicit (no Bearer token); the consent screen is operated by staff on the shared iPad, so consent submission rides the staff session — record the consenting staff actor in the audit log, not a patient session.

---

## 5. Core User Flows

### 5.1 Visit capture (surgeon / RN) — in the Gold Standard sub-tab
1. Surgeon is already signed into `/doctor/app`; opens Population Analytics → **Gold Standard**.
2. Tap **Start Visit** → calls `POST /api/gold/visits` (allocates a `gold_visit_id`, tenant-scoped), mirroring how `/api/eligibility-draft-patient` allocates a draft before upload.
3. **Consent screen** appears: staff confirms patient consent to record. Capture `consent_given` (yes/no), `consent_timestamp`, `consent_method` (`in_app_verbal` or `e_signature` with optional signature image). If **no** → abort; store only an anonymous "declined" counter; **no audio retained**.
4. Recording runs via browser `MediaRecorder` (webm/opus). A "Recording…" indicator + elapsed timer + **End Visit** button are visible. Warn on `beforeunload` (accidental refresh) — the existing portal already uses unsaved-state guards; follow the same approach.
5. Tap **End Visit** → audio uploaded to `POST /api/gold/visits/{id}/audio` → stored encrypted at rest under `$UPLOAD_DIR/gold/<tenantSlug>/<gold_visit_id>/` (same `$UPLOAD_DIR` convention as eligibility uploads). The draft pipeline (§5.4) kicks off async. Visit appears in the **Needs Review** list.

### 5.2 Surgeon review (surgeon) — the gold-label step
1. Open a visit from **Needs Review**.
2. Side-by-side: **transcript** (left), **editable draft note** (right) — reuse the existing two-column review layout and `.panel` styling from the portal.
3. Surgeon edits the note text → the edited version is the **gold note**.
4. For each correction, surgeon taps an **error tag** (taxonomy in §6): type, severity, optional corrected value. Large tap targets, no typing required for tags (iPad-friendly).
5. Surgeon confirms/enters **billing code(s)** (ICD-10 / CPT, auto-suggested by the LLM, editable) and optional **prior-auth** (drug/service, justification text, outcome: approved/denied/pending).
6. Tap **Submit** → `POST /api/gold/visits/{id}/submit` → record moves to **Needs De-ID**.
   - If the draft was perfect, a single **"Looks correct"** button submits with `error_labels: []`.
7. Track `clinician_review_seconds` (client timer, start on open, stop on submit) for ops metrics.

### 5.3 De-identification & QA (system_admin)
1. On submit, an automated PHI scrub runs on transcript + gold note (§9).
2. Operator opens the record in a QA panel: highlighted/removed PHI shown as typed placeholders; operator confirms or fixes.
3. Operator approves → `POST /api/gold/visits/{id}/approve` marks the record **export-ready**.

### 5.4 Draft pipeline (async, server-side)
1. **STT:** audio → transcript via a swappable STT provider (§8). Store raw transcript with rough speaker turns if available.
2. **Draft note:** transcript → `call_llm(role="gold_draft_note", …)` returns structured JSON: note sections (SOAP-style) + suggested ICD-10/CPT codes. The draft is scaffolding only — **never exported as truth**; only the surgeon-verified gold note is.
3. Progress streamed to the UI via SSE, reusing the `/api/eligibility-checks/{id}/stream` pattern (`status` / `result` / `error` events).

### 5.5 Export (system_admin)
1. Select export-ready records (filter by specialty, date, difficulty tags).
2. `POST /api/gold/export` generates **JSONL** (one record per line, §7) + a generated `data_dictionary.md`.
3. Download / deliver. Every export writes a provenance audit event (records included, timestamp, destination label).

---

## 6. Error-Label Taxonomy (gold labels)

Tappable error types in the review UI. Each captures severity (`low` / `medium` / `high`) and optional corrected value. **Store the taxonomy in config** (`backend/gold/taxonomy.json`, loaded the way `tuning.json` is loaded for triage) so it can be extended without code changes.

- `medication_error` — subtypes: `missed_med`, `added_med_not_discussed`, `wrong_dose`, `drug_discontinued_but_model_continued`, `wrong_frequency`
- `hallucination` — model stated something not in the conversation
- `omission` — clinically relevant content the model dropped
- `wrong_laterality_or_site`
- `diagnosis_error` — wrong or missing diagnosis
- `billing_code_error` — wrong/missing code
- `factual_value_error` — wrong lab value, vital, etc.
- `other` — free text

---

## 7. Data Model — The Exported Record (the product)

One visit → one record. Export format: **JSONL**. Canonical schema (specialty-agnostic; pilot defaults to the tenant's specialty):

```json
{
  "record_id": "tenant-gold-000142",
  "tenant_slug": "triagedm",
  "specialty": "general_surgery",
  "encounter_type": "post-op follow-up",
  "consent": {
    "consent_given": true,
    "consent_method": "in_app_verbal",
    "consent_timestamp": "2026-06-22T15:04:00Z",
    "baa_on_file": true
  },
  "deidentification": {
    "standard": "HIPAA Safe Harbor",
    "method": "automated + human QA",
    "verified_by_operator": true
  },
  "audio_metadata": {
    "duration_sec": 734,
    "difficulty_tags": ["background_noise", "translator_present", "surgical_jargon"],
    "languages": ["en", "es"]
  },
  "transcript_deid": "[PATIENT] ... [DOCTOR] ...",
  "ai_draft_note": "Continue current analgesia ...",
  "gold_note": "DISCONTINUE ... due to ...",
  "error_labels": [
    {
      "type": "medication_error",
      "subtype": "drug_discontinued_but_model_continued",
      "severity": "high",
      "section": "plan",
      "original_text": "Continue lisinopril 20mg daily",
      "corrected_text": "Discontinue lisinopril",
      "clinician_verified": true
    }
  ],
  "workflow_outputs": {
    "billing_codes": [
      { "system": "ICD-10", "code": "Z48.815", "verified_by": "clinician" }
    ],
    "prior_auth": {
      "drug_or_service": "patiromer",
      "justification_text": "[deid rationale]",
      "outcome": "approved"
    }
  },
  "clinician_review_seconds": 38,
  "clinician_id_hashed": "a91f...",
  "created_at": "2026-06-22"
}
```

Notes:
- `gold_note` + `error_labels` = the supervised-fine-tuning / evaluation payload.
- `workflow_outputs` = the conversation→workflow pairs.
- `audio_metadata.difficulty_tags` makes the dataset queryable.
- `consent` + `deidentification` blocks = the provenance buyers require.
- `clinician_id_hashed` reuses the repo's hashing approach (`hashlib.sha256`, as in `clinician_id_hashed` style hashing already used for audit/LLM input SHAs).
- `tenant_slug` is **internal provenance**; strip or pseudonymize it before delivery to a buyer per the export contract.

**Persistence:** store the working record (pre-export) in `TeamStore` (SQLite, `team.db` / `TEAM_DB_PATH`) — add a `gold_visits` table — rather than in-memory, so submitted/export-ready records survive restarts. Encrypt the free-text clinical fields at rest with the existing `field_crypto.py` helpers.

---

## 8. Tech mapping — original brief → Archangel reality (READ THIS FIRST)

| Concern | Original brief said | **Build it on Archangel as** |
|---|---|---|
| Frontend | Next.js + React + Tailwind | Static HTML/CSS/JS in `frontend/doctor.html` + `frontend/styles.css`. New panels are plain DOM, matching existing `.panel`, `.tab-btn`, `.analytics-subtab-btn`, `.table` classes. |
| Tab system | new route/page | New sub-tab under Population Analytics; wire via `setupAnalyticsSubtabs()` (§2). |
| Backend/DB | Supabase (Postgres + Auth + Storage) | FastAPI (`backend/main.py`) + new `backend/routers/gold.py` (`gold_router`, prefix `/api/gold`), registered with `app.include_router(gold_router)` next to the others (~`main.py:5859+`). Persistence via `TeamStore` (SQLite). |
| Auth/roles | Supabase Auth | Existing tenant JWT + `auth_roles.require_roles(...)` (§4). |
| Object storage | Supabase Storage (private bucket) | `$UPLOAD_DIR/gold/<tenantSlug>/<visit>/…` (same convention as `eligibility/`), encrypted at rest. |
| Recording | `MediaRecorder` → upload | Same — browser `MediaRecorder`, upload to `/api/gold/visits/{id}/audio`. |
| STT | Whisper / Deepgram behind interface | **New, swappable provider** mirroring `backend/integrations/video/{base.py,daily.py}`: add `backend/integrations/stt/{base.py, whisper.py, deepgram.py}` selected by `STT_PROVIDER` env, keys via env. (No STT exists in the repo today — this is genuinely new.) |
| Draft-note LLM | GPT-4o-class or Claude behind interface | **Already standardized on Claude** — use `ai/llm_client.call_llm(role="gold_draft_note", system=…, messages=…, prompt_id="gold_draft_note@1.0.0", patient_id=gold_visit_id, purpose="gold_draft")`. Register the role in `ai/model_config.MODEL_REGISTRY` (default `claude-sonnet-4-6`, env-overridable `MODEL_GOLD_DRAFT_NOTE`). Author the prompt in `backend/prompts/gold.py` and register it in `prompts/registry.py` so the call is auditable. |
| De-identification | Presidio and/or LLM scrubber | **New.** Default to an LLM scrubber via `call_llm(role="gold_deid", …)` returning typed placeholders; make Presidio optional behind `GOLD_DEID_PROVIDER` env (`llm` \| `presidio` \| `both`). Mandatory human QA either way. |
| Export | server fn → JSONL + dict | New endpoint in `gold_router` generating JSONL + `data_dictionary.md`. |
| Audit log | "immutable log per record" | **Already exists** — `backend/audit/audit_log.record(actor_type=…, actor_id=…, action=…, outcome=…, resource_type="gold_visit", resource=gold_visit_id, …)`. Hash-chained, append-only, `verify()`-able. LLM calls auto-log via `llm_client`. |
| Encryption at rest | required | TLS in transit (existing `http_security.py`); field-level encryption via `field_crypto.py` for clinical text; encrypt audio blobs at rest. |
| Subprocessors | BAA gating | Register STT/de-id vendors in `compliance/subprocessors.py`. |
| Tests | schema validation | `backend/tests/` (pytest), as in §13 Phase 6. |

All third-party API keys via environment variables, server-side only. No keys in `frontend/`.

---

## 9. Functional Requirements

### 9.1 Auth & accounts
- Reuse tenant JWT (`archangel_doctor_auth_token`) and `require_roles`. No new auth system.
- Re-auth daily is already the portal behavior; no change.
- Every access/edit/export writes to the existing hash-chained audit log.

### 9.2 Consent capture
- Required before any audio is retained. Store `consent_given`, `consent_method`, `consent_timestamp`, optional signature image (encrypted at rest).
- If not given, discard audio immediately; increment an anonymous tenant-scoped "declined" counter only.

### 9.3 Recording
- `MediaRecorder` → webm/opus. Upload on End Visit. Encrypt at rest. Show state + elapsed time. `beforeunload` warning.

### 9.4 Transcription (STT)
- Swappable provider (§8). Store raw transcript + rough speaker turns (doctor/patient) if available.
- Capture `audio_metadata` automatically where possible (duration); surgeon/operator-set difficulty tags (`background_noise`, `translator_present`, `accent`, `non_english`, plus specialty jargon tag).

### 9.5 Draft note generation (LLM)
- `call_llm(role="gold_draft_note", …)` with a structured prompt → JSON with note sections + suggested ICD-10/CPT. Draft is scaffolding only.

### 9.6 Surgeon review & error labeling
- Side-by-side transcript + editable note. Inline error tagging per §6; each tag stores `type`, `severity`, `section`, `original_text`, `corrected_text`, `clinician_verified: true`.
- Billing: editable code list with `system` + `verified_by`. Prior-auth optional object.
- "Looks correct" fast-path (zero edits → valid record, `error_labels: []`).
- Track `clinician_review_seconds`.

### 9.7 De-identification
- Automated PHI removal on transcript + gold note, replacing PHI with typed placeholders (`[PATIENT_NAME]`, `[DATE]`, …). Target HIPAA Safe Harbor (all 18 identifiers).
- **Mandatory human QA** (`system_admin`) before export-ready. No record exports without operator approval.

### 9.8 Data export
- Export selected export-ready records as JSONL (§7) + generated `data_dictionary.md`. Log every export for provenance.

### 9.9 Gold Standard sub-tab dashboard
- Surgeon view: visits contributed, amount earned (display only).
- Operator view (`system_admin`): queue counts (Needs Review / Needs De-ID / Export-ready) + simple QA throughput. Render with the same `.analytics-stats` / `.panel` widgets the other analytics sub-tabs use.

---

## 10. Compliance & Security (NON-NEGOTIABLE)

> Processes PHI. These are hard requirements for the pilot. They do **not** by themselves make the operation legally compliant — a signed BAA, patient consent, and legal/compliance review are prerequisites before real-patient use.

- **Consent before retention:** no audio stored without recorded consent.
- **De-identify before export:** no record leaves without automated PHI scrub **and** human QA approval, targeting HIPAA Safe Harbor.
- **Encryption:** TLS in transit; encryption at rest for audio, transcripts, and the clinical free-text fields (`field_crypto.py`).
- **Access control:** role-based via `require_roles` (§4); least privilege — `surgeon` can't export; `system_admin` operates the export pipeline.
- **Audit log:** every access/edit/export event recorded via `audit.audit_log.record(...)` with `resource_type="gold_visit"`. The chain is `verify()`-able.
- **Data retention:** raw audio deletable after STT + QA (configurable retention window via env, e.g. `GOLD_AUDIO_RETENTION_DAYS`). Store only what's needed.
- **Secrets:** all STT / de-id / LLM keys server-side via env vars (consistent with `.env.example`).
- **BAA gating:** an export cannot be marked deliverable unless `consent.baa_on_file = true` for that tenant.
- **Subprocessor honesty:** add STT and de-id vendors to `compliance/subprocessors.py`. **De-identify before sending text to any vendor that is not BAA-covered.**
- **Skills boundary:** do **not** route PHI through the dev-time Claude Code Agent Skills (per `AGENTS.md`); the server-side Skills API is not HIPAA-eligible. Use the in-product `ai/llm_client` path (BAA-covered Anthropic) only.

---

## 11. Non-Functional Requirements
- **Clinician effort:** ≤45 sec added per visit; "Looks correct" path ≤5 sec.
- **Reliability:** ~40 visits/day without data loss; safe recovery from refresh/crash mid-visit (audio chunked/uploaded on End Visit; visit row persisted in SQLite).
- **Latency:** draft note ready within ~2 min of End Visit (async; SSE progress).
- **Usability:** large tap targets, minimal typing, one-handed on iPad Safari. Match existing portal look & feel — no new design language.

---

## 12. Acceptance Criteria (Definition of Done for MVP)
- The **Gold Standard** sub-tab appears under Population Analytics on the doctor portal **only**, gated to `surgeon` (capture/review) and `system_admin` (QA/export).
- A surgeon completes capture → review → submit for a real visit in ≤45 sec of review time.
- Every submitted visit yields a schema-valid record (validates against §7).
- No record exports without consent + automated de-id + human QA approval + `baa_on_file=true`.
- "Looks correct" produces a valid record with `error_labels: []`.
- Operator exports a JSONL dataset of N records + `data_dictionary.md`.
- Every access/edit/export writes a hash-chained audit event; `audit.verify()` stays green.
- The feature runs end-to-end in iPad Safari and does **not** touch the triage `Episode.tier` pipeline.

---

## 13. Build Phases (build in this order)

**Phase 1 — Sub-tab skeleton + capture (no AI).**
- Add the Gold Standard sub-tab markup + `setupAnalyticsSubtabs()` wiring + role gating in `doctor.html`.
- `backend/routers/gold.py` (`gold_router`) with: `POST /api/gold/visits` (allocate), consent capture, `POST /api/gold/visits/{id}/audio` (upload, encrypted at rest), `GET /api/gold/visits` (list / queues). Register in `main.py`.
- Add a `gold_visits` table to `TeamStore`. Audit every endpoint.

**Phase 2 — Draft pipeline.**
- New `backend/integrations/stt/` provider interface (Whisper default). STT → transcript.
- `gold_draft_note` role in `model_config.py`, prompt in `prompts/gold.py` + registry. `call_llm` → draft note + suggested codes.
- SSE progress endpoint mirroring eligibility `/stream`. Populate the **Needs Review** queue.

**Phase 3 — Review & gold labeling.**
- Side-by-side review UI, editable gold note, tappable error taxonomy (from `gold/taxonomy.json`), billing + prior-auth capture, "Looks correct" fast-path, `clinician_review_seconds`. `POST /api/gold/visits/{id}/submit`.

**Phase 4 — De-ID & QA.**
- `gold_deid` LLM scrubber (+ optional Presidio behind env). Operator QA panel (`system_admin`), `POST /api/gold/visits/{id}/approve` → export-ready.

**Phase 5 — Export & dashboard.**
- JSONL export + `data_dictionary.md` + provenance audit. Surgeon contribution/earnings counters + operator queue stats in the sub-tab.

**Phase 6 — Hardening.**
- Confirm audit coverage (`verify()` test), encryption-at-rest checks, retention controls, BAA gating, and a `backend/tests/` schema-validation fixture set (validate exported JSONL against §7).

---

## 14. Assumptions & Open Questions
- **Assumption:** single surgical tenant; signed BAA + consent process exist outside the app (legal handled separately).
- **Assumption:** STT/LLM via BAA-covered vendors is acceptable for the pilot (Anthropic is already the BAA-covered LLM path; confirm the chosen STT vendor signs a BAA, or run de-id before sending text to it).
- **Open:** Send audio to the STT vendor as PHI (requires their BAA) or de-identify first? Default: BAA-covered vendor; flag for legal.
- **Open:** Payment/payout mechanics — tracked now, processed manually, out of MVP scope.
- **Open:** Exact specialty note template — start with generic SOAP + suggested codes; refine with the surgeon. Reuse the structured-output discipline already used by the eligibility/intra-op extractors.
- **Open:** Should export pseudonymize or fully strip `tenant_slug` and `clinician_id_hashed` for the buyer? Default: pseudonymize; confirm per buyer contract.

---

*End of PRD. This document supersedes the generic "GoldCapture" product brief for Archangel implementation purposes — wherever the two disagree on technology, this PRD (and the §8 mapping) wins.*
