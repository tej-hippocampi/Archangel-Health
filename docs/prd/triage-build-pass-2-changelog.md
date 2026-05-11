# Triage Suite — Pass 2 Remediation Changelog

Pass-2 closes the wiring gaps the reviewer identified between the four
triage stages. The algorithmic cores from the four PRDs (Initial Pre-Op,
Pre-Op Re-Tier, Intra-Op Reassessment, Post-Op Scoring) ship unchanged;
this pass adds the persistence + HTTP plumbing those algorithms always
expected to plug into.

The four PRDs themselves remain in `~/Downloads/`; this directory only
holds this changelog (per the user-confirmed scope).

## Scope decisions

- **Eligibility kept.** The eligibility router and module are pre-existing
  TEAM eligibility Track A code with 5 test files. Spec §1.1 was skipped.
- **Wound-photo pipeline deferred.** The four wound-photo contributor
  flag definitions inside `triage/postop/` stay in place but the readers
  continue to return zero/empty. No new wound-photo tables, endpoints,
  or UI.

## New routers

### `backend/routers/initial_tier.py`

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/triage/initial-tier/compute` | Pure preview |
| `POST` | `/api/episodes/{episode_id}/initial-tier` | Assign + persist (idempotent) |
| `POST` | `/api/episodes/{episode_id}/initial-tier/override` | Coordinator override (reason ≥ 30 chars) |
| `GET`  | `/api/triage/tuning/initial-tier/current` | Read tuning snapshot |
| `POST` | `/api/triage/tuning/initial-tier` | Admin no-op deploy stub |

Persistence writes onto the in-memory `_patient_store` blob:
`initial_tier`, `initial_tier_score`, `initial_tier_was_hard_escalator`,
`initial_tier_input_snapshot`, `initial_tier_reasons`,
`initial_tier_assigned_at`, `current_tier`. Audit row:
`event_logs.event_type = INITIAL_TIER_ASSIGNED`. Override audit:
`INITIAL_TIER_OVERRIDDEN`.

### `backend/routers/preop_retier.py`

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/triage/preop-retier/compute` | Pure preview |
| `POST` | `/api/episodes/{episode_id}/preop-retier/run` | Manual recompute (always writes a snapshot row) |
| `POST` | `/api/episodes/{episode_id}/pam` | Submit PAM-13 proxy + sync re-tier |
| `POST` | `/api/events/preop-video` | Video play (60-second session-id dedupe) + re-tier |
| `POST` | `/api/events/battlecard` | Battle-card view (30-minute dedupe) + re-tier |
| `GET`  | `/api/triage/tuning/preop-retier/current` | Read tuning snapshot |
| `POST` | `/api/triage/tuning/preop-retier` | Admin no-op deploy stub |

Both routers are registered in `backend/main.py` alongside the existing
intra-op and post-op routers.

## New tables

### `pam_assessments` (Pre-Op Re-Tier PRD §4.1)

```sql
CREATE TABLE IF NOT EXISTS pam_assessments (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    patient_id TEXT NOT NULL,
    responses_json TEXT NOT NULL,
    raw_sum INTEGER NOT NULL,
    items_scored INTEGER NOT NULL,
    raw_average REAL NOT NULL,
    activation_score REAL NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('LOW','MODERATE','HIGH')),
    is_complete INTEGER NOT NULL DEFAULT 0,
    model_version TEXT,
    tuning_version INTEGER,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_pam_assessments_episode_created
    ON pam_assessments(episode_id, created_at);
CREATE INDEX idx_pam_assessments_patient_created
    ON pam_assessments(patient_id, created_at);
```

CRUD methods on `TeamStore`: `save_pam_assessment(...)`,
`get_pam_assessment(id)`, `get_latest_pam_assessment(patient_id)`,
`list_pam_assessments(patient_id)`.

### `preop_retier_events` (Pre-Op Re-Tier PRD §10)

```sql
CREATE TABLE IF NOT EXISTS preop_retier_events (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    triggered_by TEXT NOT NULL,
    inputs_snapshot_json TEXT NOT NULL,
    initial_tier TEXT NOT NULL,
    initial_tier_was_hard INTEGER NOT NULL,
    computed_delta INTEGER NOT NULL,
    computed_tier TEXT NOT NULL,
    tier_before TEXT NOT NULL,
    tier_after TEXT NOT NULL,
    changed INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    model_version TEXT NOT NULL,
    tuning_version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_preop_retier_events_episode_created
    ON preop_retier_events(episode_id, created_at);
```

CRUD methods on `TeamStore`: `save_preop_retier_event(...)`,
`get_preop_retier_event(id)`, `list_preop_retier_events(patient_id)`.

## New apply orchestrator

`backend/triage/preop_retier/apply.py` — single tier-write path for the
pre-op stage. Mirrors `triage/postop/apply.py`:

- `apply_preop_retier(*, patient_id, patient_store, team_store,
  triggered_by, now=None) -> dict` — runs the full recompute, writes a
  `preop_retier_events` row, denormalizes the outcome onto the patient
  blob, emits a `PREOP_RETIER_TIER_UPDATED` or `…_RECOMPUTED_NO_CHANGE`
  audit row, and conditionally raises an `escalations` row.
- `_gather_state(...)` — pulls every signal source per Option B
  (`pam_assessments`, pre-op `survey_responses`, `event_logs` for video
  + battle-card, intake state from blob).

`backend/triage/preop_retier/locks.py` — per-episode `asyncio.Lock`
registry (`with_episode_lock(episode_id)`), mirrors
`triage/postop/locks.py`.

`backend/triage/preop_retier/patient_state.py` — blob-state helpers
mirrors `triage/postop/patient_state.py`. Exposes
`ensure_preop_retier_patient_state`, `get_initial_tier`,
`get_initial_tier_was_hard_escalator`, `update_preop_retier_denorm`,
`to_public`.

`backend/triage/preop_retier/pam_extract.py` — pure extractor that
walks intake `form_data` (flat or nested) for `pam_1`..`pam_13`
responses.

## Intake → PAM → re-tier wiring

`backend/main.py::preop_intake_submit` now calls a new helper
`_wire_intake_to_pam_and_retier(...)` after persisting the intake
submission. The helper:

1. Marks `patient.intake_status = "COMPLETE"` and stamps
   `intake_disclosures` from `extract_disclosure_flags(form_data)`.
2. Logs an `intake_completed` event.
3. Extracts PAM-13 proxy responses from `form_data` via
   `pam_extract.extract_pam_responses(form_data)`.
4. Persists a `pam_assessments` row when ≥ 1 response was extracted.
5. Calls `apply_preop_retier(triggered_by="SIGNAL:INTAKE_PAM")` inside
   `with_episode_lock(patient_id)`.

## Survey → re-tier wiring

`triage/preop_retier/apply.py::_read_surveys()` reads `survey_responses`
rows where `survey_type='preop'`, bins by `survey_day`
(`-4 → T_96`, `-2 → T_48`, `-1 → T_24`), and maps the lowercase
`green | orange | red | missed` tier to `SurveyWindowState.status`.

A T-48 or T-24 row with `red=True` / `red_flag=True` flips
`has_critical_red_flag=True`, which the algorithm picks up as the
`SURVEY_RED_FLAG_CRITICAL` hard escalator (PRD §5.2). All other
red rows stay on the soft path and emit `SURVEY_T_NN_RED` (+3).

## Architecture choice — Option B (event-stream)

Documented at the top of `backend/team_store.py`. The `episodes`
table stays minimal; per-stage state is read from event/snapshot
tables that mirror the four triage stages:

| Stage | Snapshot source |
| --- | --- |
| Initial pre-op tier | in-memory `_patient_store` blob + `event_logs INITIAL_TIER_ASSIGNED` |
| Pre-op re-tier | `pam_assessments` + `survey_responses` + `event_logs preop_video_watched` / `BATTLECARD_VIEWED` + `preop_retier_events` |
| Intra-op reassessment | `intraop_reassessments` |
| Post-op scoring | `daily_checkin_responses`, `dayx_surveys`, `med_adherence_*`, `postop_video_events`, `patient_self_flags`, `postop_retier_events` |

Two algorithm-guard fields live denormalized on the in-memory
`_patient_store` blob (where `current_tier` etc. already live):

- `initial_tier_was_hard_escalator: bool` — set by the initial-tier
  router, consumed by `triage.preop_retier.algo` for the sticky-hard
  guard.
- `post_intraop_tier: str` — set by `triage.intraop.apply`, consumed
  by `triage.postop.apply` as the immutable floor.

A future multi-process deployment would move both fields into an
`episode_snapshots` row joined off `episodes.patient_id` without
touching the algorithm cores.

## Tests added (12 files, 32 new test cases)

| File | Cases | Coverage |
| --- | ---: | --- |
| `tests/test_initial_tier_router.py` | 10 | compute / persist (idempotent) / override / tuning |
| `tests/test_preop_retier_router.py` | 10 | compute / run / PAM submit / video / battlecard / tuning |
| `tests/test_intake_pam_wiring.py` | 3 | intake-finalize → PAM → re-tier end-to-end |
| `tests/test_preop_retier_survey_wiring.py` | 4 | three-window survey reader, T-48 critical red flag short-circuit |
| `tests/test_preop_retier.py` (extended) | +2 | sticky-hard guard at delta = −5 |
| `tests/test_triage_suite_cohesion.py` (extended) | +3 | schema introspection (`preop_retier_events`, `pam_assessments`), Option B doc check, expanded patient-surface no-tier invariant for `preop-survey.html`, `preop-survey.js`, `pre-op.js` |

Total backend pytest count: 529 (up from 497 at the start of Pass 2).

## Verification checklist (spec §5)

1. `cd backend && python3 -c "from main import app; print(len(app.routes))"` → 153 (was 146 before the two new routers).
2. Each new endpoint exercised by a `TestClient` smoke test in `test_initial_tier_router.py` / `test_preop_retier_router.py`.
3. `pytest backend/tests/ -q` → 529 passed, 0 failed.
4. `pam_assessments` and `preop_retier_events` introspected from a fresh DB in `test_triage_suite_cohesion.py`.
5. Intake submit triggers `pam_assessments` + `preop_retier_events` rows (Phase 3 test).
6. T-48 red survey triggers `SURVEY_T_48_RED` contributor on the soft path (Phase 4 test); a T-48 critical red triggers `SURVEY_RED_FLAG_CRITICAL` hard escalator.
7. Patient-surface no-tier invariant: `frontend/index.html`, `frontend/postop.js`, `frontend/preop-survey.html`, `frontend/preop-survey.js`, and `frontend/pre-op.js` all clean of `current_tier`, `post_intraop_tier`, `TIER_<n>`, `tier_after`, `activation_score`, `activation_level`.
8. Direction-rule tests: post-op floor preserved by `test_apply_postop_retier_never_downgrades_below_floor` (existing); pre-op sticky-hard guard preserved by the new `test_sticky_hard_guard_blocks_minus_5_downgrade`.

## Follow-ups for v3

- Wound-photo upload, nurse-review, and training export pipeline (PRD §8).
- RPM device readings for the post-op stage.
- Care Companion engagement signals as soft contributors.
- Auto-rerun of initial tier when intake reveals new comorbidities.
- Persisted tuning store (`tuning.json` versioned per stage) to back the
  POST-tuning admin contract surfaces with real deploys.
