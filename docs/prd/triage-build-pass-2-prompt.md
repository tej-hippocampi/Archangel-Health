# Cursor Fix Prompt — Triage Build Pass 2

Paste the section below into Cursor verbatim. It is self-contained (Cursor does not have the prior chat context). Adjust only the wound-photo storage choice in §3 if you've already decided.

---

## Prompt to Cursor

You produced a 92-file build for the four-stage triage suite (Initial Pre-Op Triage, Pre-Op Re-Tier, Intra-Op Reassessment, Post-Op Scoring & Re-Tiering). A review against the four source PRDs (`docs/prd/initial-triage-v1.md`, `preop-retier-v1.md`, `intraop-reassessment-v1.md`, `postop-scoring-v1.md`, plus the integration map `docs/prd/README.md`) has found blockers, scope gaps, and integration wiring that needs to be added. Fix everything below in the order listed. Do not skip items. Do not introduce new dependencies that aren't already in the repo. After each section, run `cd backend && python3 -m pytest tests/ -q` and ensure tests pass before moving on.

The integration map at `docs/prd/README.md` is the contract that binds the four PRDs into one coherent system. Re-read §3 (conventions held across all four), §5 (cross-PRD `Episode` schema), §6 (build order), and §7 (reviewer's checklist) before you start.

### 1. Critical blockers — fix first; the app will not start until these are resolved

**1.1 `backend/main.py` imports a non-existent module.**

- Line 48: `from routers.eligibility import router as eligibility_router`
- Line 51: `from eligibility import store as elig_store`
- Line 4218: `app.include_router(eligibility_router)`
- Lines 1636–1718: roughly eighty lines of code that call `elig_store.get_check(...)` and reference `eligibility_status` / `eligibility_check_id` / `eligibility_failing_rule` keys.

Neither `routers/eligibility.py` nor an `eligibility/` package exists in the repo. The original `backend/main.py` (before your changes) does not reference eligibility at all. You introduced these references against a module that does not exist. Remove them entirely. The `/api/patients` endpoint must work without the eligibility lookups — drop the `eligibilityStatus`, `eligibilityCheckId`, and `eligibilityFailingRule` keys from the response, and remove the import lines. If the user later builds an eligibility router, they will re-add the integration in a separate change.

**1.2 No HTTP router exists for Initial Tier.**

The algorithm in `backend/triage/initial_tier.py` is correct and unit-tested, but there is no way to call it over HTTP. Per `initial-triage-v1.md` §9, create `backend/routers/initial_tier.py` exposing:

- `POST /api/triage/initial-tier/compute` — pure compute preview; takes `InitialTierInput`, returns `{ tier, score, reasons, missingDataWarnings, modelVersion, tuningVersion }`. No persistence.
- `POST /api/episodes/{episode_id}/initial-tier` — persists the computed tier on the episode + writes the snapshot fields and an `INITIAL_TIER_ASSIGNED` event to `event_logs`. Idempotent on identical inputs.
- `POST /api/episodes/{episode_id}/initial-tier/override` — coordinator override; requires `targetTier` and `reason` (≥30 chars); writes both the auto-assigned tier and the override; emits `INITIAL_TIER_OVERRIDDEN` event.
- `GET /api/triage/tuning/initial-tier/current` and `POST /api/triage/tuning/initial-tier` — current config read and admin write.

Wire the new router via `from routers.initial_tier import router as initial_tier_router` and `app.include_router(initial_tier_router)` in `main.py`.

**1.3 No HTTP router exists for Pre-Op Re-Tier.**

Same situation. The algorithm in `backend/triage/preop_retier/` is correct, but unreachable over HTTP. Per `preop-retier-v1.md` §9, create `backend/routers/preop_retier.py` exposing:

- `POST /api/triage/preop-retier/compute` — pure compute preview.
- `POST /api/episodes/{episode_id}/preop-retier/run` — persist; uses Postgres-equivalent (SQLite advisory lock or a `BEGIN IMMEDIATE` transaction) to serialize concurrent calls per `episode_id`. Always writes a `PreOpReTierEvent` row regardless of whether tier changed.
- `POST /api/episodes/{episode_id}/pam` — persists `PamAssessment` row with `responses_json`, `activation_score`, `level`, `is_complete`, `model_version`, `tuning_version`, `completed_at`. On success, triggers a synchronous re-tier.
- `POST /api/events/preop-video` — `{ episode_id, session_id, duration_sec, completed_session }`; dedupes within 60s; writes to `event_logs`; triggers re-tier.
- `POST /api/events/battlecard` — `{ episode_id, dwell_ms, scroll_depth_pct }`; dedupes within 30 minutes; writes to `event_logs`; triggers re-tier.
- `GET /api/triage/tuning/preop-retier/current` and `POST /api/triage/tuning/preop-retier`.

Register the router in `main.py`.

### 2. Required schema additions

Add to `backend/team_store.py`. Use `CREATE TABLE IF NOT EXISTS` style consistent with the rest of the file. Indexes per the PRDs.

**2.1 `pam_assessments` table** (Pre-Op Re-Tiering §13)

Columns: `id TEXT PRIMARY KEY`, `episode_id TEXT NOT NULL`, `patient_id TEXT NOT NULL`, `responses_json TEXT NOT NULL`, `raw_sum INTEGER NOT NULL`, `items_scored INTEGER NOT NULL`, `raw_average REAL NOT NULL`, `activation_score REAL NOT NULL`, `level TEXT NOT NULL CHECK(level IN ('LOW','MODERATE','HIGH'))`, `is_complete INTEGER NOT NULL DEFAULT 0`, `model_version TEXT`, `tuning_version INTEGER`, `completed_at TEXT`, `created_at TEXT NOT NULL DEFAULT (datetime('now'))`. Index on `(episode_id, created_at)`.

**2.2 `preop_retier_events` table** (Pre-Op Re-Tiering §13)

Columns: `id TEXT PRIMARY KEY`, `episode_id TEXT NOT NULL`, `triggered_by TEXT NOT NULL`, `inputs_snapshot_json TEXT NOT NULL`, `initial_tier TEXT NOT NULL`, `initial_tier_was_hard INTEGER NOT NULL`, `computed_delta INTEGER NOT NULL`, `computed_tier TEXT NOT NULL`, `tier_before TEXT NOT NULL`, `tier_after TEXT NOT NULL`, `changed INTEGER NOT NULL`, `reasons_json TEXT NOT NULL`, `model_version TEXT NOT NULL`, `tuning_version INTEGER NOT NULL`, `created_at TEXT NOT NULL DEFAULT (datetime('now'))`. Index on `(episode_id, created_at)`.

**2.3 Episode-level snapshot — choose one approach and apply consistently**

The integration README §5 lists ~25 fields the PRDs specify on `Episode`. The existing repo uses a minimal `episodes` table and an event-stream pattern (`event_logs`). Pick one and document the choice in a top-of-file comment in `team_store.py`:

- **Option A (PRD-literal):** Add all snapshot columns to `episodes` via `ALTER TABLE`. Use the column names from the integration README §5. Provides cheap queue-display lookups.
- **Option B (event-stream, current direction):** Keep `episodes` minimal; require all snapshots to be readable from the snapshot tables (`*_events`, `intraop_reassessments`, `pam_assessments`). Adds query overhead but stays consistent with the existing storage idiom.

Whichever is chosen, the following two episode columns must be present, because the algorithms read them as guards:

- `initial_tier_was_hard_escalator INTEGER NOT NULL DEFAULT 0` — read by pre-op re-tier sticky guard.
- `post_intraop_tier TEXT` — read by post-op re-tier as the floor it cannot drop below.

Without those two columns, `triage/preop_retier/algo.py` cannot enforce the sticky-hard-escalator guard and `triage/postop/algo.py` cannot enforce its floor. Both PRD invariants depend on them.

### 3. Wound-photo dual-purpose pipeline — restore the omitted scope

You explicitly omitted the wound-photo upload + nurse-review pipeline. The author's directive in chat was to build it, and Post-Op PRD §8 (the entire section) is the spec. Restore it now.

**3.1 Schema**

Add to `team_store.py`:

- `wound_photos` — `id TEXT PRIMARY KEY`, `episode_id TEXT NOT NULL`, `patient_id TEXT NOT NULL`, `photo_blob_url TEXT NOT NULL`, `patient_note TEXT`, `submitted_at TEXT NOT NULL DEFAULT (datetime('now'))`. Index on `(episode_id, submitted_at)`.
- `wound_photo_reviews` — `id TEXT PRIMARY KEY`, `wound_photo_id TEXT NOT NULL`, `reviewed_by TEXT NOT NULL`, `reviewed_at TEXT NOT NULL DEFAULT (datetime('now'))`, `is_problematic TEXT NOT NULL CHECK(is_problematic IN ('YES','NO','UNABLE_TO_ASSESS'))`, `concern_types_json TEXT`, `unable_reasons_json TEXT`, `severity TEXT`, `action_taken_json TEXT NOT NULL`, `explanation TEXT NOT NULL`, `confidence INTEGER NOT NULL`, `clinical_context_json TEXT NOT NULL`. Index on `reviewed_at`.

**3.2 Endpoints in `routers/postop.py`**

- `POST /api/episodes/{episode_id}/wound-photo` — multipart upload; accepts JPEG/HEIC/PNG up to 12 MB; rejects other MIME types with 415; writes `wound_photos` row; emits `WOUND_PHOTO_SUBMITTED` event into `event_logs`; triggers a synchronous post-op re-tier.
- `GET /api/episodes/{episode_id}/wound-photos` — list with thumbnails.
- `POST /api/wound-photos/{wound_photo_id}/review` — RN review; takes the structured review form payload (per PRD §8.3) and writes a `wound_photo_reviews` row. Snapshot the patient's clinical context at review time (procedure family, days post-op, current tier) into `clinical_context_json`. Require `explanation` ≥30 chars.

**3.3 Re-tier signal**

The post-op re-tier already references `WOUND_PHOTO_SUBMITTED_BY_D5`, `WOUND_PHOTO_SUBMITTED_BY_D10`, `WOUND_PHOTO_NOT_SUBMITTED_BY_D7`, `WOUND_PHOTO_NOT_SUBMITTED_BY_D14` in the audit-flag and positive-contributor lists. Verify your engagement reader (`backend/triage/postop/scoring/`) reads from the new `wound_photos` table to compute these. Photo content is **not** read by re-tier in v1 — only the binary submission timestamps. Do not add any model-driven wound classifier.

**3.4 Nurse review form UI**

Add a structured review form to the doctor / RN dashboard (`frontend/doctor.html` is the appropriate surface). The form must include all fields from PRD §8.3: is-problematic radio, concern-type multi-select, unable-reasons multi-select, severity radio, action-taken multi-select, explanation textarea (≥30 chars), confidence slider 0–100. Submit posts to `POST /api/wound-photos/{id}/review`.

**3.5 Patient upload UI**

Add a "Wound photos" section to the patient post-op surface (`frontend/postop.js` and the patient HTML). Native camera/file picker; multipart upload to the new endpoint; clear post-submit confirmation.

**3.6 Nightly de-identified training-data export**

Add a cron loop (in `backend/triage/postop/cron.py` plus a startup task in `main.py`) that runs nightly and writes a redacted parquet snapshot of `wound_photo_reviews` to `wound-photo-training/<yyyy-mm-dd>.parquet`. Use stable salted hashes for `patient_id`. Do not copy photo binaries — only URLs. Make the export opt-in per institution via a feature flag in `tuning.json`.

### 4. Wire the existing intake form to PAM scoring

The intake form parser (`backend/intake_form_parser.py`) and intake interview (`backend/intake_section_chat.py`) already exist. Per `preop-retier-v1.md` §4.3, the PAM-style proxy must be embedded as section 3.5 of the intake interview, and the result must persist to `pam_assessments`.

In the intake submission handler (find the existing endpoint that finalizes the intake form — likely in `main.py`), after the existing parsing logic, add:

1. Extract the section-3.5 PAM responses from the parsed form.
2. Call `from triage.preop_retier.pam_proxy import score_pam` (verify exact name in your code) to compute the result.
3. Write the result to `pam_assessments` via a new helper in `team_store.py`.
4. Trigger `POST /api/episodes/{id}/preop-retier/run` (or call the algorithm directly in-process).

Add a unit test that verifies submitting an intake form with PAM section yields a `pam_assessments` row and triggers a re-tier event.

### 5. Wire the existing pre-op surveys to pre-op re-tier

`backend/preop_survey.py` already scores T-96/T-48/T-24 surveys and writes to `survey_responses` with `tier` (green/orange/red). The pre-op re-tier must consume that output as a soft contributor.

In `backend/triage/preop_retier/delta.py` (or wherever the soft delta is computed), confirm the reader pulls the most recent `survey_responses` row per window for the patient and maps `tier` to the contributor: `green=0`, `orange=+1`, `red=+3`, `missed=+2`. Add a unit test that exercises this path end-to-end: simulate three submitted survey rows, run pre-op re-tier, assert the contributors fire correctly.

### 6. Verification checklist — run before declaring done

Use `docs/prd/README.md` §7 (the reviewer's checklist) as the master list. The high-priority items:

1. `from main import app` succeeds and the FastAPI startup runs without exceptions.
2. `pytest backend/tests/ -q` passes; `test_triage_suite_cohesion.py` runs (it was previously blocked by the eligibility import).
3. Every endpoint listed in §1.2, §1.3, §3.2 above returns a 2xx for happy-path requests.
4. `pam_assessments` and `preop_retier_events` tables exist after a fresh DB init.
5. `wound_photos` and `wound_photo_reviews` tables exist; uploading a JPEG via the new endpoint writes both `wound_photos` and an `event_logs` row.
6. Submitting a complete intake form writes a `pam_assessments` row and emits a `PREOP_RETIER_TIER_UPDATED` (or `_RECOMPUTED_NO_CHANGE`) event.
7. Submitting a T-48 survey with a red tier triggers a pre-op re-tier with the corresponding soft contributor in the reasons.
8. Patient-facing HTML/JS files contain zero direct renders of `tier`, `score`, `activation_score`, or `activation_level`. Grep for those identifiers in `frontend/index.html`, `frontend/postop.js`, `frontend/preop-survey.js`, `frontend/preop-survey.html`. The only places these may appear are doctor/admin surfaces (`frontend/doctor.html`, `frontend/admin.html`).
9. Intra-op + post-op re-tier never algorithmically downgrade. Confirm with focused tests: a clean post-intra-op TIER_3 patient with perfect engagement stays TIER_3; the post-op delta is unsigned (no negative arithmetic) per Post-Op PRD §10.3.
10. Pre-op re-tier sticky-hard guard works: when `initial_tier_was_hard_escalator=1`, a delta ≤ −3 does not downgrade.

### 7. Out of scope for this pass — do not build

- A wound-photo content classifier (v2 PRD; the data pipeline is the only v1 deliverable).
- RPM device readings; keep `rpm_enabled: false` in `tuning.json`.
- Care Companion engagement; keep `care_companion_enabled: false`.
- Auto-rerun of initial tier when intake reveals new comorbidities (manual coordinator advisory only in v1).
- An eligibility router; remove the broken references and let a future PRD add eligibility cleanly.

### 8. Definition of done

- `docs/prd/README.md` §7 reviewer's checklist passes.
- `pytest backend/tests/ -q` reports the same or higher passing count than before this change, with `test_triage_suite_cohesion.py` included.
- All five PRD-mandated routers exist (initial-tier, preop-retier, intraop, postop, plus the existing admin tuning-read endpoints) and are registered in `main.py`.
- Patient app surfaces never render tier, score, or activation values.
- The wound-photo pipeline is operable end-to-end: patient uploads → RN reviews → labeled training row exists → nightly export writes a parquet file.

When done, write a one-page changelog at `docs/prd/triage-build-pass-2-changelog.md` listing exactly what changed, which tests were added, and any follow-ups left for a v3 pass.
