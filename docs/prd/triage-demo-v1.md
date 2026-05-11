# Triage Escalation Demo PRD v1.0

> **Audience:** Cursor (build agent).
> **Goal:** Build a second, isolated demo account that showcases the **Triage Escalation** product. The existing demo (`manan.vyas@cedarssinai.com` / `ArchangelDemo2024!`) is reserved for the Patient Education & Resources walkthrough and **must not be touched**.
> **Meeting context:** Live walkthrough with Anne Tumilson. The demo must be deterministic, fast, and visually polished — no flaky API spinners, no stale data after restart.

---

## 1. Scope summary

Build a **separate, fully-seeded demo tenant** that demonstrates, end-to-end:

1. **RN Care Coordinator** working a queue of 10 TEAM-eligible patients with varied triage tiers and risk drivers.
2. **Live TEAM-eligibility + initial-tier prediction** when the RN adds a new 11th patient on-stage.
3. **Pre → Post-op transition** via the intra-op notes flow, with surgeon confirmation and a real-time tier delta.
4. **Post-op resource generation** for the transitioned patient using the standard post-discharge notes flow.
5. **Patient "I need help"** self-flag that escalates and surfaces an alert to the correct staff roles in real time.
6. **Explainable risk** — every tiered patient (pre and post) carries a human-readable reason chain ("why this tier").

Out of scope: changes to the existing `CDRSNAI1` / `manan.vyas` demo, changes to landing marketing UI, schema migrations beyond what's required for the new tenant.

---

## 2. The two new demo accounts

A **single new tenant** (`ARCH_TRIAGE_DEMO`, clinic_code `TRIAGEDM`, display name "Archangel Triage Demo Clinic") with two seeded staff members:

| Role | Email | Password | Display name | Notes |
|---|---|---|---|---|
| `surgeon` (TEAM director) | `dr.thompson@archangeldemo.com` | `TriageDemo2025!` | "Dr. Eleanor Thompson, MD" | Auto-seeded as director on tenant creation. Has access to surgeon-only routes: intra-op `READY_FOR_SURGEON_REVIEW` lock, `switch-to-postop`, manual surgeon escalation. |
| `rn_coordinator` | `rn.castillo@archangeldemo.com` | `TriageRN2025!` | "Maria Castillo, RN" | RN queue, intra-op draft (NEW / IN_PROGRESS / REOPENED), self-flag resolution, manual re-tier requests. |

Seeding rules (see existing `_ensure_demo_doctor` in `backend/main.py` for the pattern):

* Both rows are upserted on every startup so passwords stay deterministic — credentials must never drift between restarts.
* `clinic_code = "TRIAGEDM"`, `tenant_slug = "archangel-triage-demo"`, `health_system_code = "TRIAGEDM"`.
* The surgeon row gets `is_team_director = True`.
* Both staff records carry `office_phone = "(310) 555-0200"` and `hospital_affiliations = "Archangel Triage Demo Clinic"`.
* The tenant must be discoverable in `staff_context` resolution and pass JWT role checks in `auth_roles.require_roles` for `surgeon` and `rn_coordinator` exactly as in production.

**Acceptance test:** logging in with either credential pair hits the doctor portal at `/doctor` and lands the user inside the TRIAGEDM tenant view — and only sees TRIAGEDM patients, never CDRSNAI1's.

---

## 3. Seeded patient roster (10 patients, all TEAM-eligible)

All 10 patients must be:

* `tenant_slug = "archangel-triage-demo"`, `clinic_code = "TRIAGEDM"`, `health_system_id = ARCH_TRIAGE_DEMO_HEALTH_SYSTEM_ID`.
* **TEAM-eligible** — meaning their stored eligibility check has `verdict = SAVE_AS_TEAM` and all six TEAM checks (Part A, Part B, MA, MSP, ESRD, UMWA) green-lit. Hydrate this via the existing `team_store` finalize path so the audit log shows them as TEAM-eligible.
* On a TEAM anchor procedure family (`LEJR`, `CABG`, `SPINAL_FUSION`, `HIP_FEMUR_FRACTURE`, or `MAJOR_BOWEL`).
* Carrying a populated `initial_tier` plus a non-empty `reasons[]` array (see §6).

Split: **5 pre-op, 5 post-op.** Distinct tiers and distinct dominant risk drivers — no two patients should "look the same" on the RN queue.

| # | Name | Phase | Procedure | Family | Tier | Score | Dominant risk driver |
|---|---|---|---|---|---|---|---|
| 1 | Robert Chen | pre_op | Total Knee Arthroplasty | LEJR | TIER_1 | 1 | Clean — age 64, BMI 27, no escalators |
| 2 | Patricia Alvarez | pre_op | Total Hip Arthroplasty | LEJR | TIER_2 | 5 | BMI 38 + current smoker (10 pack-years) |
| 3 | Michael O'Brien | pre_op | Spinal Fusion L4–L5 | SPINAL_FUSION | TIER_2 | 6 | Active opioid use + lives alone, no caregiver |
| 4 | Linda Whitfield | pre_op | CABG x3 | CABG | TIER_3 | — | HARD: CHF within 30 days |
| 5 | David Mensah | pre_op | Sigmoidectomy | MAJOR_BOWEL | TIER_2 | 7 | Functional status: PARTIALLY_DEPENDENT + diabetes uncontrolled (HbA1c 9.8) |
| 6 | Helen Park | post_op | Total Knee Arthroplasty | LEJR | TIER_1 | 1 | Day 4 post-op, on track |
| 7 | Jamal Carter | post_op | CABG x4 | CABG | TIER_2 | 5 | Day 6 post-op, low EF (35%) + at-risk alcohol use |
| 8 | Sandra Reyes | post_op | Hip Femur Fracture ORIF | HIP_FEMUR_FRACTURE | TIER_3 | — | HARD: ventilator-dependent intra-op finding |
| 9 | Gregory Tate | post_op | Spinal Fusion T11–L1 | SPINAL_FUSION | TIER_2 | 6 | ASA 3 + recent fall + housing UNSTABLE |
| 10 | Yolanda Brooks | post_op | Colectomy | MAJOR_BOWEL | TIER_2 | 4 | Age 77 + BMI 32 + lives alone |

For every patient, persist:

* `_patient_store[pid]` blob with `name`, `phone`, `email`, `pipeline_type`, `structured_data` (incl. `procedure_name`, `procedure_date`, `surgeon_name = "Dr. Eleanor Thompson, MD"`), `clinic_code`, `resource_code`, `resources` (preop or diagnosis+treatment depending on phase), `office_phone`, `tenant_slug`.
* `team_store.episodes` row with `procedure_type`, `clinic_code = "TRIAGEDM"`, `open_date` (today for pre-op; today − N days for post-op where N matches the demo day in the table above).
* `episode_snapshots` row with `initial_tier`, `initial_tier_was_hard_escalator`, and (for post-op patients) `post_intraop_tier`.
* An `INITIAL_TIER_ASSIGNED` event in `event_logs` with the full `reasons[]` payload (see §6).
* Pre-op patients with a TIER_2/3 tier additionally get a `PREOP_RETIER` event so the RN queue's "last re-tier" column lights up.
* Post-op patients get one or two `daily_checkin_responses` rows shaped to support their displayed tier (e.g. Sandra Reyes carries a pain spike + missed wound photo, Jamal Carter carries one BP outlier).

Procedure dates: pre-op patients scheduled 3–14 days from today; post-op patients on day 2, 4, 6, 8, and 10 of their episode respectively.

**Acceptance test:** RN logs in → sees exactly these 10 patients in the queue, sorted by tier descending, with the dominant risk driver visible in the row hover/expanded view.

---

## 4. Live "Add Patient" flow with real-time tier + eligibility (Demo Moment 1)

The user will click **"Add Patient"** from the RN queue and submit a minimal intake form on-stage. The page must show:

1. **TEAM eligibility check status** streaming live via SSE (`/api/eligibility-checks/{id}/stream`), reaching `SAVE_AS_TEAM` in **≤ 4 seconds** with all six checks green.
2. **Initial-tier prediction** rendered immediately on intake submit via `POST /api/triage/initial-tier/compute` (stateless preview) and then committed via `POST /api/episodes/{id}/initial-tier` once the user accepts.
3. A visible **"Why this tier?"** card showing the `reasons[]` chain (HARD escalator OR `PROCEDURE_BASE + Σ soft weights = score → tier`).

To make this deterministic in demo mode:

* When `DEMO_MODE=1` and `clinic_code == "TRIAGEDM"`, the eligibility extractor must use a **canned `SAVE_AS_TEAM` fixture** instead of calling Anthropic. A new fixture file `backend/eligibility/fixtures/demo_triage_team.json` provides the canonical "all six checks green" payload.
* The SSE stream still emits real `status` / `result` events, just with deterministic timing (insert four ~700ms `status` ticks: `parsing` → `extracting` → `evaluating` → `result`).
* The intake form pre-fills a suggested 11th-patient template (name: "Anne Tumilson", procedure: "Total Knee Arthroplasty (LEJR)", DOB: 1956-03-12, Medicare A+B, no exclusions) so the demo flow is one click. The user can edit any field; the eligibility extractor still returns the SAVE_AS_TEAM fixture as long as `clinic_code == "TRIAGEDM"`.
* On commit, the tier engine actually runs against the entered fields (so the surgeon will see a *real* TIER_1/2/3 reflecting whatever was typed). For the suggested defaults the expected outcome is **TIER_1, score 1, reason chain "LEJR base risk (1)"**.

**Acceptance test:** From a clean RN session, clicking "Add Patient" → autofill → "Run eligibility & tier" displays `SAVE_AS_TEAM` and `TIER_1` within 5 seconds, with the `reasons[]` chain rendered as a vertical list under "Why this tier?".

---

## 5. Pre → Post-op transition (Demo Moment 2)

The RN selects **patient #3, Michael O'Brien** (pre-op, Spinal Fusion, TIER_2). The flow:

1. RN clicks **"Mark OR started"** → `set_or_started_at` fires, patient phase moves to `intra_op`.
2. RN opens **intra-op form** (`/api/episodes/{id}/intraop-form`) and types the first batch of notes. Suggested seeded autofill (RN-pasteable) lives in `frontend/intraop-form.html` as a "Demo notes" button and contains:
   * OR duration: 215 min (above LEJR p90 — already a soft escalator).
   * EBL: 850 mL.
   * Unanticipated dural tear repaired primarily.
   * Intra-op transfusion: 2 units PRBC.
3. RN clicks **"Mark Ready for Surgeon Review"** → status flips to `READY_FOR_SURGEON_REVIEW`. (RN routes are now locked out; surgeon routes unlock.)
4. **Switch to surgeon login** (Dr. Thompson). The surgeon dashboard shows the form awaiting confirmation with a prominent toast/badge.
5. Surgeon reviews, optionally tweaks, clicks **"Confirm & Switch to Post-Op"** → `POST /api/episodes/{id}/switch-to-postop` (surgeon-only).
6. **Real-time tier update** — the intra-op delta engine recomputes `Episode.tier`, persists `post_intraop_tier`, and fires a server-side event the RN UI listens to (existing SSE or a new lightweight WebSocket/polling tick — pick the lowest-risk option). The RN side should visibly transition O'Brien from **TIER_2 → TIER_3** with a flash animation and an inline reason explaining the bump (OR duration > p90, EBL > 500mL, unanticipated dural tear).

Reason chain for the demo: `PRIOR_PREOP_TIER (TIER_2)` + `OR_DURATION_OVER_P90` + `EBL_OVER_500ML` + `UNANTICIPATED_INTRAOP_EVENT:dural_tear` + `INTRAOP_TRANSFUSION` → TIER_3.

**Acceptance test:** Both browser windows update within 3 seconds of the surgeon's "Confirm" click. Re-loading either tab shows the same TIER_3 state. The audit log shows the full causal chain: RN drafts → surgeon confirms → tier delta computed.

---

## 6. Explainable risk — "Why this tier?" everywhere

The triage engine already returns `TierAssignment.reasons[]` (`HARD` | `BASE` | `SOFT`, with `code`, `label`, `weight`). This data is currently emitted but not consistently surfaced in the RN queue.

For the demo, every patient row in the RN queue and every patient detail page must render an inline **"Why this tier?"** card with:

* For HARD escalator: a red badge naming the escalator (e.g. *"CHF within 30 days — automatic TIER_3"*).
* For soft-scored tiers: a stacked-bar visualization of `BASE` + each `SOFT` contribution, with hover/click revealing the `label` and `weight`. Totals to `score` and references the `tier3_min` / `tier2_min` thresholds with a tick mark on the bar.
* For post-op tiers: same surface, but the contributing rows include `PRIOR_PREOP_TIER`, intra-op events, daily check-ins, surveys, adherence, self-flags — all already named in `triage/postop/`.

The card data source: `GET /api/episodes/{id}/triage-explain` — **new lightweight read endpoint** that returns the most recent tier event with its reasons, plus any contributing intra-op / post-op deltas since. It's a fan-in over existing tables (`event_logs INITIAL_TIER_ASSIGNED`, `preop_retier_events`, `intraop_reassessments`, `postop_retier_events`); no new persistence.

**Acceptance test:** Clicking any patient on the queue opens a detail panel where the "Why this tier?" card matches the table in §3 exactly. Linda Whitfield shows a single red HARD badge; Michael O'Brien shows a stacked bar with three soft contributions adding to 6.

---

## 7. Post-op resource generation for the transitioned patient (Demo Moment 3)

After O'Brien is switched to post-op (§5), the surgeon clicks **"Generate post-op resources"**. This calls the existing `POST /api/process-patient` pipeline using the same post-discharge notes shape as production:

* Discharge diagnosis: "Status post L4–L5 instrumented posterior spinal fusion with intra-op dural tear repair."
* Treatment plan: lumbar brace 6 wks, narcotic + NSAID step-down, neuro checks q4h x 48h, no driving x 2 wks, follow-up in 10 days.

The generator produces the **diagnosis** and **treatment** resources (battlecard HTML + voice script). These hydrate `_patient_store["o_brien_id"]["resources"]["diagnosis"]` and `["treatment"]` exactly as today.

In demo mode with `clinic_code == "TRIAGEDM"`, gracefully degrade if `ANTHROPIC_API_KEY` is missing by emitting a pre-canned "Spinal Fusion — Recovery Plan" battlecard + voice script (already templated by `_build_demo_battlecard`). Cursor must add a fixture for spinal-fusion post-op to the demo blueprint so the demo never fails offline.

**Acceptance test:** "Generate post-op resources" produces both battlecards within 8 seconds. The patient detail page now shows both the diagnosis card and the treatment card. No 5xx, no spinner stuck.

---

## 8. Patient "I need help" → real-time staff escalation (Demo Moment 4)

After resources are generated, the user opens **O'Brien's patient dashboard** (`/patient/{o_brien_id}`) in a third tab, taps the **"I need help"** self-flag tile (existing UI in `frontend/postop.js` and `frontend/index.html`), and types "Severe new lower-back pain radiating down right leg, worse than yesterday."

System behavior:

1. `POST /api/episodes/{id}/postop/self-flag` fires (patient-session route, no Bearer required).
2. `create_self_flag` writes to `patient_self_flags`; `_trigger_retier` immediately recomputes post-op tier and persists a `postop_retier_events` row.
3. New tier: TIER_3 (semantic-escalation soft factor + self-flag + already TIER_3 floor from intra-op).
4. **Alert routing:**
   * **`rn_coordinator`** sees an in-app banner + the queue row turns red and floats to the top, within 2 seconds (poll interval or SSE).
   * **`surgeon`** sees a less aggressive secondary notification (badge on the patient card), since alert *resolution* is `rn_coordinator`-only per `auth_roles.WRITE_CLINICAL` policy but the surgeon needs visibility.
   * `np_pa` not in scope for this demo tenant (no seed user).
5. RN clicks the alert → opens the patient → clicks **"Acknowledge"** → `POST /api/episodes/{id}/postop/self-flag/resolve` fires. Banner clears, queue row drops back to normal color but stays at TIER_3.

**Acceptance test:** The self-flag round-trip from patient tap → RN alert visible → RN acknowledge → patient sees "Care team notified" confirmation completes in under 5 seconds end-to-end.

---

## 9. Implementation plan (file-by-file)

| File | Change |
|---|---|
| `backend/main.py` | Add `_ensure_triage_demo_staff()` (mirrors `_ensure_demo_doctor`) for both new accounts. Add `_triage_demo_patient_blueprint()` returning the 10-patient roster from §3. Add `_seed_triage_demo()` that wires `_patient_store`, `team_store.ensure_episode`, `event_logs`, `episode_snapshots`, and `daily_checkin_responses`. Wire into the existing demo-mode startup hook, **guarded by `clinic_code == "TRIAGEDM"`** so it never collides with CDRSNAI1. |
| `backend/auth.py` | No behavioral change; passwords flow through the existing `register_user` path. |
| `backend/eligibility/fixtures/demo_triage_team.json` | New fixture: full SAVE_AS_TEAM payload, all six checks green. |
| `backend/eligibility/extractor.py` (or pipeline) | Add a `_demo_short_circuit(clinic_code)` branch that returns the fixture above for `TRIAGEDM`, with synthetic SSE timing. |
| `backend/routers/initial_tier.py` | No new endpoint; existing `/api/triage/initial-tier/compute` and `/api/episodes/{id}/initial-tier` already cover the live preview + commit. |
| `backend/routers/episodes.py` (or extension of `initial_tier.py`) | New `GET /api/episodes/{id}/triage-explain` returning the most recent tier event + contributing deltas (read-only fan-in, no schema change). |
| `backend/routers/intraop.py` | No new routes — `switch-to-postop` and the RN/surgeon status gating already exist and must be exercised verbatim. |
| `backend/routers/postop.py` | No code change required if the existing `/postop/self-flag` + `/self-flag/resolve` round-trip is wired into the RN queue UI's alert subscription. If the queue today does not auto-refresh on a new self-flag, add a lightweight SSE on `GET /api/tenants/{slug}/alerts/stream` (or extend an existing stream) emitting `self_flag_created` events. |
| `frontend/doctor.html` + supporting JS | Render the "Why this tier?" card on every queue row's expand and on the patient detail panel. Wire the alerts banner / floating red row to the new SSE feed. Add the "Add Patient" autofill button with the §4 default payload. |
| `frontend/intraop-form.html` | Add a "Demo notes" autofill button (only visible when `clinic_code == "TRIAGEDM"`) inserting the §5 note batch. |
| `frontend/postop.js` / `index.html` | The "I need help" tile is already present; verify the self-flag posts and the patient sees a clear "Care team notified" confirmation toast within 1 second. |
| `backend/tests/test_triage_demo.py` | New tests covering: (a) both demo accounts authenticate; (b) all 10 patients seed with the expected tiers and reason chains; (c) `/triage-explain` returns the right shape; (d) the intraop → postop transition raises O'Brien to TIER_3 with the expected reasons. |

Do **not** change:

* The existing `_ensure_demo_doctor` block, `manan.vyas@cedarssinai.com`, the CDRSNAI1 clinic, or any of its 49 seeded patients.
* The Patient Education / battlecard / voice-avatar pipeline for non-TRIAGEDM patients.
* Any production triage, intraop, or postop algorithm — the demo runs the real engines, not mocks (apart from the eligibility extractor's optional offline fixture).

---

## 10. Demo-day operational checks (must pass before walkthrough)

1. `DEMO_MODE=1` + backend boots with no Anthropic / ElevenLabs / Tavus keys → still completes the full §4 → §8 flow with canned audio/text fallbacks.
2. Logging in as either new account lands in TRIAGEDM and shows only the 10 seeded patients.
3. Restarting the backend preserves the 10 patients (via `demo_patient_store.json` snapshot path that already handles CDRSNAI1) and re-seeds idempotently.
4. The RN queue UI sorts by tier desc, then by `tier_last_changed` desc. Linda Whitfield (TIER_3 HARD) and Sandra Reyes (TIER_3 HARD, post-op) sit at the top.
5. Adding the 11th patient never persists across restart in non-`DEMO_PERSIST` mode — keep the seed deterministic.
6. Self-flag round-trip is observable in `/admin/audit/eligibility` and `event_logs` for forensic playback.

---

## 11. Non-goals / explicit deferrals

* No production-grade alert routing rules — surgeon visibility is a single read of unresolved self-flags on the tenant; full priority queue logic stays as-is.
* No new tier algorithm tuning. Existing `triage.tuning`, `triage.intraop.tuning`, `triage.postop.tuning` are correct; the demo just exercises them with curated data.
* No mobile-specific UI — assume the user demos on a desktop browser.
* No NP/PA persona in this demo; the codebase still supports the role for non-demo tenants.

---

## 12. Credentials at a glance (copy/paste for the demo)

```
Tenant:        Archangel Triage Demo Clinic
Clinic code:   TRIAGEDM

Surgeon (TEAM director)
  email:       dr.thompson@archangeldemo.com
  password:    TriageDemo2025!

RN Care Coordinator
  email:       rn.castillo@archangeldemo.com
  password:    TriageRN2025!
```

Existing (untouched) Patient Education demo:
```
  email:       manan.vyas@cedarssinai.com
  password:    ArchangelDemo2024!
```
