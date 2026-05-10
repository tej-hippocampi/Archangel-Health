# Cursor Fix Prompt — Triage Build Pass 3

Paste the section below into Cursor verbatim. It is self-contained.

---

## Prompt to Cursor

Pass 2 closed most of the wiring gaps. This pass tightens four things you reported as deferred or unverified, adds the Care Companion post-op signal path the user explicitly asked for, and introduces a "revised tier after intake" snapshot the user asked for. Source PRDs remain `docs/prd/initial-triage-v1.md`, `preop-retier-v1.md`, `intraop-reassessment-v1.md`, `postop-scoring-v1.md`, with `docs/prd/README.md` as the integration map. Pass-2 changes you made are not being undone — this pass is a delta.

After each section, run `cd backend && python3 -m pytest tests/ -q` and ensure the pass count is at or above what you reported at the end of pass 2 (529).

### 1. Verify the eligibility-router situation in the working tree

Pass-2 changelog says: "The eligibility router and module are pre-existing TEAM eligibility Track A code with 5 test files. Spec §1.1 was skipped." That assertion is not consistent with the user's working tree — `find . -path '*/eligibility*'` in `backend/` returns nothing. Either you ran pass 2 against a branch with the eligibility code that hasn't landed in the user's tree yet, or the assertion was incorrect and `from main import app` is still broken on the broken eligibility imports.

Do all of the following:

1. From the user's repo root, run `find backend -name 'eligibility*' -o -path '*/eligibility/*'`. If output is empty, the module is not present.
2. Run `python3 -c "import sys; sys.path.insert(0, 'backend'); from main import app; print(len(app.routes))"`. If this raises `ModuleNotFoundError: No module named 'eligibility'` or `routers.eligibility`, the eligibility code is genuinely absent.
3. If absent: either bring the eligibility module into this branch (and document its provenance — which branch / commit / source) OR remove the four broken references in `backend/main.py` exactly as the pass-2 prompt §1.1 instructed: import lines around 48 and 51, the `app.include_router(eligibility_router)` call, and the eligibility lookups in the `/api/patients` handler around lines 1636–1718.
4. If present: leave alone. Add a one-sentence note to `docs/prd/triage-build-pass-2-changelog.md` clarifying the provenance.

The shippable end-state is: `from main import app` succeeds without manual setup beyond what is committed. Do not leave the user holding a broken import.

### 2. Persist the two algorithm-guard fields

Pass-2 stored `initial_tier_was_hard_escalator` and `post_intraop_tier` on the in-memory `_patient_store` blob. **Server restart wipes both fields.** That breaks two PRD invariants:

- `triage/preop_retier/algo.py` reads `initial_tier_was_hard_escalator` to enforce the sticky-hard downgrade guard. After restart, the flag reads `False`, the guard silently disengages, and a Tier-3-by-hard-escalator patient becomes downgradeable.
- `triage/postop/algo.py` reads `post_intraop_tier` as the floor it cannot drop below. After restart, the floor reads `None`, the post-op tier may be computed against a missing floor, and the upward-only invariant is at risk.

Fix by persisting both fields. Two acceptable approaches; pick one and document the choice in the top-of-file comment in `team_store.py`:

- **Option A (preferred):** Add a new `episode_snapshots` table keyed by `patient_id` (or `episode_id`) holding `initial_tier_was_hard_escalator INTEGER NOT NULL DEFAULT 0`, `post_intake_tier TEXT`, `post_intraop_tier TEXT`, plus `updated_at TEXT`. Read-through writes from the in-memory blob on every set; reads fall back to the table on a cold start.
- **Option B:** Add the two columns directly to `episodes` via `ALTER TABLE episodes ADD COLUMN ... IF NOT EXISTS`-equivalent (SQLite doesn't have `IF NOT EXISTS` for `ADD COLUMN`; use a try/except or schema-version check).

Whichever is chosen, on every router/orchestrator that currently sets the field on `_patient_store`, also write through to the persistent store. On every read in the algorithms, prefer the persistent store and fall back to the blob. Add a regression test that:

1. Sets `initial_tier_was_hard_escalator=True` via the initial-tier persist endpoint.
2. Clears the in-memory `_patient_store` (simulating a process restart).
3. Re-reads via the algorithm and confirms the flag is still `True`.

Same test pattern for `post_intraop_tier` after an intra-op lock.

### 3. Verify PAM-13 questions are actually surfaced to patients

`triage/preop_retier/pam_extract.py` walks intake `form_data` for `pam_1`..`pam_13` keys. If those keys are never written by the intake interview, every patient submission yields `is_complete=False` and the re-tier penalizes them with `PAM_NOT_COMPLETED_BY_T_72` (+2) and `PAM_NOT_COMPLETED_BY_T_24` (+3) — effectively punishing patients for not completing an instrument they were never asked.

Do all of the following:

1. Audit `backend/intake_section_prompts/` (or wherever the intake interview prompts live) for a section 3.5 PAM block. The 13 items are listed verbatim in `docs/prd/preop-retier-v1.md` §4.1. If the items are absent, add them as a structured section that the patient progresses through during the intake interview, on a 4-point scale (1=Strongly Disagree to 4=Strongly Agree, plus N/A).
2. Confirm the parser writes the responses into `form_data` under the exact keys `pam_1` through `pam_13` that `pam_extract.py` expects. If the parser writes them under different keys (e.g., `readiness_1`, `pam.q1`, etc.), align the extractor and the parser to the same key shape.
3. Add an end-to-end test: simulate an intake interview that completes section 3.5 with 13 valid responses; confirm `pam_assessments` row has `items_scored=13` and `is_complete=True`; confirm the `apply_preop_retier` event reasons do **not** contain `PAM_NOT_COMPLETED_BY_T_72`.
4. Add a defensive test for the inverse: an intake submission that never includes any `pam_*` key still produces a `pam_assessments` row with `is_complete=False` and the re-tier emits the not-completed penalty exactly once (no double-penalty bugs).

If for some clinical-product reason the PAM section will not be presented to patients in v1, gate the not-completed penalty behind a `pam_proxy_in_scope: true` flag in the tuning block, default `false`, so absent responses are not punished. Default to surfacing the questions — that is the user's intent.

### 4. Care Companion post-op risk signals — build them now

The user has explicitly asked that Care Companion engagement be used to evaluate post-op risk. Strong prior art exists in the repo, so this is wiring, not new infrastructure:

- `backend/main.py` already has `_evaluate_semantic_escalation_llm(message, conversation_history)` which returns `{"tier": 0 | 2 | 3, "trigger_type": "semantic", "reason": "..."}` — an LLM-based classifier of patient chat messages.
- `event_logs` already records an `avatar_chat` event per chat session.
- The `/api/digital-care-companion/chat` endpoint at `backend/main.py:3004` is the entry point.

#### 4.1 Persist the semantic escalation result

Currently `_evaluate_semantic_escalation_llm` returns its verdict inline within the chat response. Persist it. On every chat turn that yields a tier-2 or tier-3 verdict:

- Write a new `event_logs` row with `event_type='care_companion_semantic_escalation'`, payload `{ tier, reason, message_excerpt, conversation_id }`. Truncate `message_excerpt` to 500 chars.
- These events are the post-op re-tier signal source for the Care Companion path.

#### 4.2 Add reader and contributor flags

In `backend/triage/postop/scoring/`, add a `care_companion.py` module exposing:

- `count_chat_sessions_last_7d(patient_id)` — counts distinct `avatar_chat` events in the last 7 days.
- `latest_semantic_escalation(patient_id)` — returns the most recent `care_companion_semantic_escalation` event within the post-op window, or `None`.
- `count_chat_sessions_total(patient_id)` — for the inactivity check.

In `backend/triage/postop/delta.py` (or the contributor-emitting module), add:

- **Hard escalator:** if `latest_semantic_escalation(...)` is unresolved and `tier == 3`, fire `CARE_COMPANION_RED_FLAG_TIER_3` → forces post-op tier to TIER_3, equivalent semantically to `NEW_RED_FLAG_SYMPTOM`. Mirror the existing red-flag reason payload so the alert pipeline (`escalations` row) treats it identically.
- **Soft contributor:** if `latest_semantic_escalation(...)` returned tier 2 within the last 24h and is unresolved, add `+2` with reason `CARE_COMPANION_SEMANTIC_ESCALATION_TIER_2`.
- **Engagement audit (no tier effect):** if `count_chat_sessions_last_7d(...) >= 2`, add `CARE_COMPANION_ACTIVE_LAST_7D` to the audit-flag list (post-op delta is unsigned per Post-Op PRD §10.3.b).
- **Soft contributor:** if `count_chat_sessions_total(...) == 0` and the episode is past D7, add `+1` with reason `CARE_COMPANION_NEVER_USED_BY_D7`.

Resolution semantics: a semantic-escalation event is "resolved" when an `escalations` row referencing the same `conversation_id` has `resolved=True`. Until resolved, the contributor keeps firing on every re-tier.

#### 4.3 Tuning flip + cohesion test

- In `tuning.json` (or wherever the in-code tuning lives, since the persisted tuning store is deferred), set `care_companion_enabled: true` for post-op.
- Add tests in `tests/test_postop_care_companion.py`:
  1. Tier-3 semantic escalation forces post-op tier to TIER_3.
  2. Tier-2 semantic escalation adds the +2 soft contributor and is overridden by other hard escalators when present.
  3. Active engagement (≥2 chat sessions in 7d) emits the audit-only flag.
  4. Zero engagement past D7 emits the +1 contributor exactly once.
  5. Resolution of the escalation row removes the contributor on the next re-tier.

#### 4.4 Patient-facing surface unchanged

No tier or risk signal is shown to the patient. The chat response continues to behave as it does today. The signal flow is server-side only.

### 5. Post-intake revised tier — distinct snapshot

The user wants a clearly-named "tier after intake" snapshot that is distinct from both `initial_tier` (immutable, computed at upload) and `current_tier` (rolling, may change on every signal). It is NOT a rerun of `assign_initial_tier`.

Implementation:

1. Add a `post_intake_tier TEXT` field to whichever persistent store you chose in §2 (`episode_snapshots` row OR `episodes` column).
2. In `_wire_intake_to_pam_and_retier` (the post-intake handler), after `apply_preop_retier(triggered_by="SIGNAL:INTAKE_PAM")` returns, check if `post_intake_tier` is currently `None`. If yes, set it to the freshly computed `current_tier`. Subsequent re-tier calls do **not** overwrite this snapshot.
3. The snapshot is taken once per episode, at the moment the intake first triggers a re-tier. If the patient updates intake later, `post_intake_tier` is preserved as-is; the live `current_tier` continues to evolve normally.
4. Emit a `POST_INTAKE_TIER_SNAPSHOTTED` event into `event_logs` with the snapshot value and the contributing reasons.

#### 5.1 Doctor / admin UI surface

In `frontend/doctor.html`, add a column or chip on the patient row showing the three tier values when they exist:

```
Tier at upload: T1   →   Tier after intake: T2   →   Current: T2
```

Use the same color convention (TIER_3 = red, TIER_2 = amber, TIER_1 = neutral). When `post_intake_tier` differs from `initial_tier`, surface a small indicator that intake completion changed the tier. When `current_tier` differs from `post_intake_tier`, surface a separate indicator that signals after intake have moved the tier further.

In `frontend/admin.html` Triage Logic tab, add the same three-tier display to the patient detail panel.

#### 5.2 No patient-facing display

Confirm none of `initial_tier`, `post_intake_tier`, `post_intraop_tier`, or `current_tier` leak to patient surfaces. Re-run the existing patient-surface invariant test with the new field name added to the grep list.

#### 5.3 Test coverage

Add `tests/test_post_intake_tier_snapshot.py`:

1. Upload a patient with no hard escalators (initial_tier=TIER_1). Submit an intake that introduces enough soft contributors to push to TIER_2. Confirm `post_intake_tier=TIER_2`, `initial_tier=TIER_1`, `current_tier=TIER_2`.
2. After step 1, simulate a signal that pushes `current_tier` to TIER_3. Confirm `post_intake_tier` is still TIER_2 (not overwritten), `initial_tier` is still TIER_1, `current_tier=TIER_3`.
3. Submit a second intake (rare but legal). Confirm `post_intake_tier` is still the value from the first intake (snapshot is once-per-episode).

### 6. Out of scope for this pass — do not build

- The wound-photo upload, nurse-review pipeline, and de-identified training-data export. Still deferred. Wound-photo contributor flag definitions in the post-op re-tier code stay in place; their readers continue to return zero/empty.
- RPM device readings; keep `rpm_enabled: false` in the tuning block.
- Persisted tuning store backed by versioned `tuning.json`. The POST tuning admin endpoints stay as no-op stubs.
- Auto-rerun of `assign_initial_tier` (the user explicitly said no). The post-intake revised tier in §5 is the substitute and is the correct shape.

### 7. Definition of done

- `from main import app` succeeds against the user's working tree without ModuleNotFoundError.
- `pytest backend/tests/ -q` reports ≥ 529 passing (the pass-2 baseline) plus the new tests added in §2, §3, §4, §5.
- `initial_tier_was_hard_escalator` and `post_intraop_tier` survive a simulated process restart, verified by regression test.
- An end-to-end test from "complete intake with PAM section" to "re-tier event fired with PAM contributor reasons present" passes.
- A tier-3 Care Companion semantic escalation drives post-op tier to TIER_3 and writes the corresponding `event_logs` row.
- `post_intake_tier` is set exactly once per episode and is visible distinctly from `initial_tier` and `current_tier` on doctor and admin surfaces.
- Patient-facing HTML/JS files contain zero direct renders of `initial_tier`, `post_intake_tier`, `post_intraop_tier`, `current_tier`, `tier_after`, `activation_score`, or `activation_level`.

When done, append to `docs/prd/triage-build-pass-2-changelog.md` (or create a sibling `triage-build-pass-3-changelog.md`) listing what changed in this pass, which tests were added, the eligibility verification result, and any follow-ups left for v4.
