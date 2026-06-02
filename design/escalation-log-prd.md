# PRD — Escalation Log Redesign & Triage Drilldown

**Product:** Archangel Health — Doctor Portal
**Surface:** Escalation Log tab (`frontend/doctor.html`, `#tab-escalation`)
**Status:** Ready for build
**Owner:** (you)
**Audience for this doc:** Cursor / implementing engineer

---

## 1. Summary

Redesign the Doctor Portal **Escalation Log** so each escalation is a clean, scannable card showing only the three things a clinician needs at a glance — **patient name**, **episode phase (Pre-Op / Post-Op)**, and **risk tier (color-coded by urgency)** — with a single **"View Triage"** action.

"View Triage" opens a detail panel that tells the full risk story for that patient:
1. **Current risk tier** and the exact date/time they reached it.
2. A **longitudinal timeline** of how their risk evolved (e.g. Tier 1 → Tier 3), labeled by clinical phase (pre-op, after the intra-operative procedure, during post-op).
3. The **granular reasons** — the specific findings — that moved them between tiers.
4. A **"Send Intervention"** action that emails the patient directly.

Escalations must be **sorted by tier descending** (Tier 3 at top, then Tier 2, then Tier 1) regardless of when they occurred, so the most urgent patients are always on top.

**Good news for scope:** the backend already persists everything needed (tier history, reasons, phase timestamps, patient email, provider identity, and a working email transport). This is **mostly a read/aggregate + UI build**, plus **one new write endpoint** for the intervention email. No changes to the triage scoring engine.

---

## 2. Goals & Non-Goals

### Goals
- A doctor can open the Escalation Log and instantly triage by urgency (tier-sorted).
- Each card communicates name + phase + tier with zero clicks.
- One click reveals the complete, human-readable risk journey for that patient.
- A doctor can send an urgent message to the patient by email without leaving the panel.
- The design is visually clean, calm, and unambiguous — built for fast clinical reading.

### Non-Goals (out of scope for this build)
- Changing how tiers are computed (the triage engine is untouched).
- SMS / push interventions (email only for v1; structure code so SMS can be added later).
- Two-way messaging / patient replies (this is a one-way outbound message).
- Editing or resolving escalations is **retained as-is** (do not remove the existing resolve toggle — see §5.4).
- Real-time updates / websockets.

---

## 3. Current State (what exists today)

**Frontend** — `frontend/doctor.html`
- Escalation page markup: lines ~1208–1219 (`#tab-escalation`, `#escalationList`, `#resolvedCounter`).
- Escalation CSS (`.esc-*`, `.escalation-head`, `.yn-btn`): lines ~494–528.
- "View Conversation" chat modal: lines ~1566–1575 (`#chatModal`, `#chatTitle`, `#chatBody`).
- Render logic in `loadEscalations()`: lines ~3404–3447. Uses an `apiJson(path, opts)` fetch helper, `esc()` HTML-escaper, `fmt(iso)` date formatter, `episodePillHtml()`, `inferEpisodeType()`, `prettyTrigger()`, and a module-level `PATIENTS` array + `escalations` array.
- Existing tier color tokens to reuse (lines ~256–276, ~1085–1101): `.tier-chip.tier-t1` (green), `.tier-t2` (amber), `.tier-t3` (red).

**Backend** — `backend/main.py`, `backend/team_store.py`
- `GET /api/escalations` (main.py ~2339–2385) returns:
  ```json
  {
    "escalations": [{
      "id": 42, "patient_id": "pat_xyz", "patient_name": "John Doe",
      "tier": 3, "trigger_type": "chat:semantic", "origin": "Chat",
      "message": "...", "consent": null, "consent_at": null,
      "resolved": false, "created_at": "2025-06-02T14:32:10",
      "conversation_snapshot": [{"role": "patient", "content": "..."}]
    }],
    "resolved_count": 5, "total_count": 12, "filter_applied": "surgeon_tier3_only"
  }
  ```
  Note: escalation `tier` is an **integer 1/2/3**.
- `PATCH /api/escalations/{id}/resolved` (main.py ~2388–2399), body `{"resolved": bool}`.
- Escalations table: `team_store.py` ~120–131 (+ `health_system_id` migration ~551).

**Triage history (already persisted — this is the data source for the timeline):**
- `preop_retier_events` — `team_store.list_preop_retier_events(patient_id, limit=200)`. Columns include `triggered_by`, `tier_before`, `tier_after`, `changed`, `reasons_json`, `created_at`.
- `intraop_reassessments` — `team_store.list_intraop_reassessments(patient_id)`. Columns include `pre_or_current_tier`, `proposed_tier`, `final_tier`, `hard_upgrade_applied`, `reasons_json`, `triggered_at`, `triggered_by`.
- `postop_retier_events` — `team_store.list_postop_retier_events(patient_id, limit=200)`. Columns include `triggered_by`, `tier_before`, `tier_after`, `changed`, `reasons_json`, `created_at`.
- `episode_snapshots` — cold-start floors: `initial_tier`, `initial_tier_reasons`, `post_intraop_tier`, plus assignment timestamps.
- Patient blob (`_patient_store[patient_id]`) carries `current_tier`, `initial_tier`, `tier_last_changed`, `phase`/`pipeline_type`, `or_started_at`, `or_ended_at`, `discharge_at`, `procedure_date`. Triage tiers here are **strings `"TIER_1" | "TIER_2" | "TIER_3"`**.

**Reason object shape** (inside every `reasons_json`):
```json
{ "kind": "HARD|SOFT|BASE|POSITIVE|ENGAGEMENT_AUDIT|INFO",
  "code": "DAY7_RED_SURVEY", "label": "Patient scored RED on Day 7 survey.",
  "weight": 5, "detail": "day7_red_flag: True" }
```

**Email transport (already works):**
- `backend/email_utils.py` → `async def send_html_email(to_email, subject, html_body, *, importance_headers=False) -> bool`. SendGrid primary, SMTP fallback, `EMAIL_DEV_MODE=1` prints to stdout. `is_email_transport_configured() -> bool`.
- Patient email: `_patient_store[patient_id]["email"]`.
- Provider identity: `StaffContext` (`backend/staff_context.py`) → `.name`, `.role` (`surgeon | rn_coordinator | np_pa | system_admin`), `.tenant_id`. Institution name via `team_store.get_health_system_by_id(staff.tenant_id)["name"]`.

**Stack:** FastAPI + raw-SQL SQLite (`team_store.py`); vanilla HTML/JS frontend served static; run with `cd backend && python3 -m uvicorn main:app --reload --port 8000`; tests in `backend/tests/` via `pytest`.

---

## 4. Tier model & reconciliation (IMPORTANT)

Two tier representations exist:
- **Escalation rows** use integer tier `1 | 2 | 3`.
- **Triage engine / patient blob** uses string `"TIER_1" | "TIER_2" | "TIER_3"`.

**Rule for this feature:** the card's tier badge and the sort order use the patient's **current triage tier** (`_patient_store[patient_id]["current_tier"]`), because that is the clinically live risk level and matches what the triage drilldown shows. **Fallback:** if a patient blob has no `current_tier`, derive from the escalation's integer `tier` (`1→TIER_1`, etc.).

Implement a single normalizer used everywhere:
```
normalize_tier(x) -> 1 | 2 | 3
  "TIER_3" -> 3, "TIER_2" -> 2, "TIER_1" -> 1, 3 -> 3, "3" -> 3, etc.
```

---

## 5. UX Specification

### 5.1 Escalation card (the box)

Each escalation renders as one card in `#escalationList`. The card shows **exactly three labeled things** plus the action:

```
┌─────────────────────────────────────────────────────────────┐
│  Maria Gonzalez                              [ View Triage ]  │
│  ● Post-Op                                                    │
│  ┌──────────┐                                                 │
│  │ TIER 3   │   ← large, color-coded, high-emphasis           │
│  └──────────┘                                                 │
└─────────────────────────────────────────────────────────────┘
```

**Content:**
1. **Name** — `patient_name`, prominent (bold, ~16px).
2. **Episode phase** — a pill reading **"Pre-Op"** or **"Post-Op"**, derived from the patient's `phase`/`pipeline_type` (reuse existing `episodePillHtml()` / `inferEpisodeType()`).
3. **Tier** — a **large, color-coded badge** ("TIER 3"). This is the visual anchor of the card.

**Tier color system (reuse existing tokens for consistency):**
| Tier | Background | Border | Text | Signal |
|------|-----------|--------|------|--------|
| Tier 3 | `#fef2f2` | `#fecaca` | `#b91c1c` | High urgency (red) |
| Tier 2 | `#fffbeb` | `#fde68a` | `#b45309` | Moderate (amber) |
| Tier 1 | `#ecfdf5` | `#a7f3d0` | `#047857` | Low (green) |

The Tier 3 badge should read as visually heavier than Tier 1 — larger weight, red. Optionally add a subtle left border accent on the whole card matching the tier color (Tier 3 cards get a `4px` red left border, mirroring the existing `.roster-row.tier-3-patient` pattern at doctor.html ~285–289) to make Tier 3 pop in the list.

**Action:** a **"View Triage"** button in the **top-right** of the card.

**Removed from the card face** (vs. today): the inline `Tier N • date`, `Origin:`, `Trigger:`, and `Consent:` meta lines. These details move into the triage detail panel (§5.2) so the card stays clean. (Consent/origin/trigger are still shown inside the panel for reference.)

### 5.2 "View Triage" detail panel

Clicking **View Triage** opens a modal/drawer (reuse the existing `.modal-overlay`/`.modal` pattern; create a new `#triageModal`). It is populated from a new endpoint (§6.1). Layout top → bottom:

**A. Header**
- Patient name + Pre-Op/Post-Op pill.
- **Current risk tier**, large and color-coded, with the line: **"Tier 3 since Jun 9, 2026, 8:15 AM"** (the timestamp the patient reached the current tier = `current_tier_since`).

**B. Risk timeline (longitudinal)**
A vertical, chronological timeline (oldest → newest) of every tier-defining event. Each node shows:
- **Timestamp** (date + time).
- **Phase label**: `Pre-Op`, `Intra-Op (in OR)`, `After Intra-Op Procedure`, or `Post-Op — Day N`.
- **Tier transition**: `TIER_1 → TIER_3` rendered with the two color-coded chips and an arrow. Nodes where the tier did **not** change but a reason was recorded may be shown as muted "assessment" nodes (de-emphasized) — see §8 for filtering rules.
- A one-line summary (`triggered_by` humanized, e.g. "Day 7 survey").

The phase label must make the user's required distinction clear: **"was the change after the intra-operative procedure, or during the post-op episode?"** Use `or_ended_at` and `discharge_at` to classify (see §6.2).

**C. Why it changed (granular findings)**
Under each timeline node that changed the tier, list the specific findings from `reasons_json`, each as a readable row:
- `label` as the primary text (e.g. "Patient scored RED on Day 7 survey").
- `detail` as muted secondary text when present.
- A small `kind` tag (HARD / SOFT / etc.) and `weight` if you want to show contribution (optional, keep subtle).

Hard escalators (`kind: "HARD"`) should be visually emphasized (red dot / "Critical finding" tag) since they auto-drive Tier 3.

**D. Footer action**
- A **"Send Intervention"** button (primary, prominent). Clicking it reveals the intervention composer (§5.3).
- Keep a secondary **"View Conversation"** link that shows the existing `conversation_snapshot` (preserves today's functionality).

### 5.3 "Send Intervention" composer

Opens inline within the triage panel (or a secondary modal). For v1 this sends an **email to the patient**.

**Fields:**
- **To** (read-only): patient's email (`_patient_store[patient_id]["email"]`). If the patient has no email, disable Send and show "No email on file for this patient."
- **Subject** (read-only, auto-generated — see exact format below).
- **Message** (textarea, required): free text the provider writes.
- **Send** button (primary) + **Cancel**.

**Subject line format (exact):**
```
[Provider Name], [Provider Role], [Institution] — URGENT CARE MESSAGE
```
- Provider Name = `staff.name` (fallback to `staff.email`).
- Provider Role = display-mapped: `surgeon → "Surgeon"`, `rn_coordinator → "RN Coordinator"`, `np_pa → "NP/PA"`, else titlecased role.
- Institution = `get_health_system_by_id(staff.tenant_id)["name"]`, fallback `"Archangel Health"`.

Example: `Dr. Jane Smith, Surgeon, Cedars-Sinai Medical Center — URGENT CARE MESSAGE`

**Send behavior:**
- POST to new endpoint (§6.3). Backend builds an HTML email (wrap the provider's message in a simple branded shell — reuse the style approach in `backend/onboarding_emails.py`) and calls `send_html_email(to_email, subject, html_body, importance_headers=True)`.
- On success: show success toast ("Intervention sent to patient"), disable the Send button (mirror existing `.btn.sent` pattern), and log the event.
- On failure / email not configured: show error toast with the reason (e.g. "Email is not configured" when `is_email_transport_configured()` is false → backend returns 503).

### 5.4 Resolve toggle (retain)

Keep the existing **"Escalation Resolved: Y / N"** control and the `#resolvedCounter` ("X/Y Escalations Resolved"). It can live at the bottom of the card or inside the triage panel — implementer's choice — but its behavior and the `PATCH /api/escalations/{id}/resolved` call must remain unchanged.

### 5.5 Sorting (hard requirement)

`#escalationList` must be sorted by **tier descending**: all **Tier 3** cards first, then **Tier 2**, then **Tier 1** — independent of `created_at`. Within the same tier, sort by `created_at` **descending** (most recent first). Tier is the patient's current triage tier per §4.

---

## 6. Backend Specification

### 6.1 New endpoint — triage timeline

```
GET /api/escalations/{escalation_id}/triage-timeline
```
(Equivalently keyed by patient; using the escalation id keeps the frontend call simple since the card already has it. Resolve `patient_id` from the escalation server-side.)

**Auth:** same clinical-staff guard as `GET /api/escalations`, and reuse `_assert_clinical_staff_can_access_patient()`.

**Response:**
```json
{
  "patient_id": "pat_xyz",
  "patient_name": "Maria Gonzalez",
  "episode_phase": "post_op",
  "current_tier": 3,
  "current_tier_since": "2026-06-09T08:15:00",
  "surgery": {
    "procedure_date": "2026-06-01",
    "or_started_at": "2026-06-01T08:00:00",
    "or_ended_at": "2026-06-01T11:30:00",
    "discharge_at": "2026-06-02T12:00:00"
  },
  "timeline": [
    {
      "at": "2026-05-28T10:30:00",
      "phase": "PRE_OP",
      "phase_label": "Pre-Op",
      "tier_before": null,
      "tier_after": 1,
      "changed": true,
      "triggered_by": "INITIAL_ASSESSMENT",
      "source": "initial",
      "reasons": [
        {"kind": "BASE", "code": "HIP_BASE", "label": "Hip/femur fracture base risk", "weight": 1, "detail": null}
      ]
    },
    {
      "at": "2026-06-01T11:30:00",
      "phase": "INTRA_OP",
      "phase_label": "After Intra-Op Procedure",
      "tier_before": 1, "tier_after": 2, "changed": true,
      "triggered_by": "SURGEON_LOCK",
      "source": "intraop",
      "reasons": [
        {"kind": "SOFT", "code": "INTRAOP_BP_VASOPRESSOR", "label": "BP instability requiring vasopressors", "weight": 6, "detail": null}
      ]
    },
    {
      "at": "2026-06-09T08:15:00",
      "phase": "POST_OP",
      "phase_label": "Post-Op — Day 7",
      "tier_before": 2, "tier_after": 3, "changed": true,
      "triggered_by": "SURVEY_D7",
      "source": "postop",
      "reasons": [
        {"kind": "HARD", "code": "DAY7_RED_SURVEY", "label": "Patient scored RED on Day 7 survey", "weight": 85, "detail": "day7_red_flag: True"}
      ]
    }
  ]
}
```

**Construction (server-side aggregation — no schema changes):**
1. Look up escalation → `patient_id`. Pull patient blob + `episode_snapshots`.
2. Seed timeline with the **initial assessment** node (`initial_tier` + `initial_tier_reasons` + assignment timestamp), `source: "initial"`, `tier_before: null`.
3. Append all `list_preop_retier_events(patient_id)` → `source: "preop"`.
4. Append all `list_intraop_reassessments(patient_id)` → `source: "intraop"`; map `pre_or_current_tier`→`tier_before`, `final_tier`→`tier_after`, `triggered_at`→`at`.
5. Append all `list_postop_retier_events(patient_id)` → `source: "postop"`.
6. Normalize every tier to int 1/2/3 (§4). Parse each `reasons_json`.
7. Sort ascending by `at`.
8. Compute `phase` + `phase_label` per §6.2.
9. `current_tier` = patient `current_tier` (normalized); `current_tier_since` = `tier_last_changed` (fallback to the `at` of the last node whose `changed == true`).

### 6.2 Phase labeling logic

For each event timestamp `at`, given `or_started_at`, `or_ended_at`, `discharge_at`:
- `source == "intraop"` → `INTRA_OP`, label **"After Intra-Op Procedure"** (these reassessments are stamped at/after OR close).
- else if `or_ended_at` exists and `at < or_started_at` (or no OR yet) → `PRE_OP`, label **"Pre-Op"**.
- else if `or_started_at <= at <= or_ended_at` → `INTRA_OP`, label **"Intra-Op (in OR)"**.
- else if `at > or_ended_at` and `at < discharge_at` → label **"After Intra-Op Procedure"** (still inpatient, post-procedure).
- else if `discharge_at` exists and `at >= discharge_at` → `POST_OP`, label **"Post-Op — Day N"** where `N = floor((at - discharge_at)/1 day) + 1`.
- Fallbacks: if timestamps are missing, label by `source` (`preop → "Pre-Op"`, `postop → "Post-Op"`).

### 6.3 New endpoint — send intervention email

```
POST /api/escalations/{escalation_id}/intervention
```

**Auth:** clinical-staff guard + `_assert_clinical_staff_can_access_patient()`.

**Pydantic model** (define near `EscalationResolveRequest`, main.py ~1463):
```python
class EscalationInterventionRequest(BaseModel):
    message: str   # required, the provider's free-text body
```

**Behavior:**
1. Resolve escalation → `patient_id` → patient email. If no email → `409` with detail `"No email on file for this patient."`.
2. If `not is_email_transport_configured()` → `503` detail `"Email transport is not configured."`.
3. Build subject from `StaffContext` per §5.3 exact format.
4. Build HTML body wrapping `message` in a minimal branded shell (lead line identifying sender + institution, the message, a confidentiality footer). Reuse styling approach from `onboarding_emails.py`.
5. `ok = await send_html_email(patient_email, subject, html_body, importance_headers=True)`.
6. On success, log an audit event: `team_store.log_event(patient_id=..., event_type="provider_intervention_email", payload={"escalation_id": id, "subject": subject, "provider_email": staff.email, "message_excerpt": message[:500]})`.
7. Return `{"ok": true}` (or `502`/`{"ok": false}` if `send_html_email` returns False).

**Validation:** reject empty/whitespace `message` with `400`.

### 6.4 Optional helper on `GET /api/escalations`

To avoid an extra round-trip per card, optionally enrich each escalation in the existing list response with `current_tier` (normalized int) and `episode_phase` pulled from the patient blob. If you prefer minimal backend change, the frontend can instead read the existing module-level `PATIENTS` array to resolve current tier + phase by `patient_id` (the render code already does `PATIENTS.find(p => p.id === e.patient_id)`). **Recommended:** enrich server-side for a single source of truth.

---

## 7. Files to touch

**Backend**
- `backend/main.py`
  - Add `EscalationInterventionRequest` model (~near 1463).
  - Add `GET /api/escalations/{id}/triage-timeline` (place near the other escalation routes ~2339–2411).
  - Add `POST /api/escalations/{id}/intervention` (same area).
  - Optionally enrich `GET /api/escalations` items with `current_tier` + `episode_phase` (~2339–2385).
  - Add a small `_provider_email_signature(staff)` + `_normalize_tier(x)` helper (or put normalizer in a shared util).
- `backend/team_store.py` — **no schema change required.** Reuse `list_preop_retier_events`, `list_intraop_reassessments`, `list_postop_retier_events`, `get_escalation`, `log_event`, `get_health_system_by_id`.
- `backend/email_utils.py` — reuse `send_html_email` / `is_email_transport_configured`. (Optionally add an `build_intervention_email(...)` helper in a new or existing email-template module.)

**Frontend** — `frontend/doctor.html`
- CSS block (~494–528): replace/extend `.esc-*` styles for the new card; add tier-badge classes, `#triageModal` timeline styles, intervention composer styles. Reuse `.tier-chip` tokens (~256–276).
- Markup: add a `#triageModal` overlay alongside `#chatModal` (~1566–1575).
- `loadEscalations()` (~3404–3447): new card template (name + phase pill + tier badge + "View Triage"); apply §5.5 sort; wire "View Triage" → fetch `/api/escalations/{id}/triage-timeline` → render panel; keep resolve toggle + counter; keep "View Conversation".
- Add `renderTriagePanel(data)` and `sendIntervention(escalationId, message)` functions using the existing `apiJson` helper.

**Tests** — `backend/tests/`
- New `test_triage_timeline.py`: build a patient with seeded pre/intra/post events, assert merged/sorted timeline, phase labels, and `current_tier_since`.
- New `test_intervention_email.py`: with `EMAIL_DEV_MODE=1`, assert subject format, 409 on missing email, 503 when unconfigured, 400 on empty message, and an audit event is logged.

---

## 8. Edge cases & rules
- **No history:** if a patient has only the initial assessment, the timeline shows a single node. Never error on empty event tables.
- **Noise reduction:** the timeline may include many `changed == false` assessment events. Default to showing only nodes where `changed == true` **plus** the initial node; provide a subtle "Show all assessments" toggle to reveal the rest. (Implementer may show all if simpler, but changed-only is the default view.)
- **Tier normalization** must handle both int and `"TIER_n"` everywhere (§4).
- **Missing timestamps:** fall back to `source`-based phase labels (§6.2). Never crash on null `or_ended_at`/`discharge_at` (pre-op patients won't have them).
- **Missing patient blob:** fall back to escalation integer tier for the card badge/sort; triage panel shows "Limited history available."
- **Sorting stability:** equal tier → newest `created_at` first.
- **Email not configured locally:** dev should set `EMAIL_DEV_MODE=1` so Send "succeeds" and prints to stdout for testing.
- **Authorization:** every new endpoint must enforce the same staff access checks as existing escalation endpoints; surgeons retain the Tier-3-only filter behavior already present on `GET /api/escalations`.
- **HTML safety:** escape all patient/provider text rendered in the panel with the existing `esc()`; the intervention message must be HTML-escaped before being placed in the email body.

---

## 9. Acceptance Criteria
1. Escalation Log shows one clean card per escalation with **only** name, Pre-Op/Post-Op pill, and a color-coded tier badge, plus a top-right **View Triage** button.
2. Tier 3 badges are red and visually dominant; Tier 1 are green; Tier 2 amber.
3. Cards are ordered **Tier 3 → Tier 2 → Tier 1**, newest-first within a tier, regardless of creation time.
4. **View Triage** opens a panel showing current tier + "since <date/time>".
5. The panel renders a chronological risk timeline with phase labels that correctly distinguish pre-op vs. after-intra-op vs. post-op (verified against `or_ended_at`/`discharge_at`).
6. Each tier change lists the specific findings (`label`/`detail`) that drove it; hard escalators are visibly flagged.
7. **Send Intervention** emails the patient with subject exactly `「Provider Name」, 「Role」, 「Institution」 — URGENT CARE MESSAGE` and the provider's typed body; success/failure surfaces as a toast.
8. The existing resolve toggle + resolved counter and "View Conversation" still work.
9. New endpoints enforce existing auth; no triage scoring logic changed.
10. Backend tests for timeline aggregation and intervention email pass under `pytest` with `EMAIL_DEV_MODE=1`.

---

## 10. Implementation order (suggested)
1. Backend `GET /triage-timeline` (aggregator + phase labeler) + test.
2. Backend `POST /intervention` (email) + test.
3. (Optional) enrich `GET /api/escalations` with `current_tier`/`episode_phase`.
4. Frontend card redesign + tier badges + sort.
5. Frontend triage panel (header → timeline → reasons).
6. Frontend intervention composer + toasts.
7. Manual run-through: `cd backend && python3 -m uvicorn main:app --reload`, open `/doctor/app`, verify against §9.

---

## Appendix A — Role display map
```
surgeon         -> "Surgeon"
rn_coordinator  -> "RN Coordinator"
np_pa           -> "NP/PA"
system_admin    -> "Administrator"
(other)         -> role.replace('_',' ').title()
```

## Appendix B — Reason `kind` → display
```
HARD            -> "Critical finding"  (red emphasis; auto-Tier-3 driver)
SOFT / BASE     -> contributing factor (neutral)
POSITIVE        -> improvement/positive signal
ENGAGEMENT_AUDIT-> engagement note (muted)
INFO            -> context (muted, may hide)
```
