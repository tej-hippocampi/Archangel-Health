# Cursor Prompt — Escalation Log & "View Triage" Risk-Timeline Redesign + Phase-Label Fix

> Paste everything below the line into Cursor. It targets the current codebase
> (FastAPI backend in `backend/`, static `frontend/doctor.html` with inline
> CSS+JS, no build step, in-memory data). Make surgical edits that match the
> existing vanilla-JS / template-string style. Do not add frameworks or a build
> step.

---

You are working in the Archangel/CareGuide repo. All of the doctor-facing
escalation + triage UI lives in **`frontend/doctor.html`**; the triage timeline
data is built server-side in **`backend/main.py`**
(`GET /api/escalations/{id}/triage-timeline`). The demo data is seeded in
**`backend/triage_demo_seed.py`**. Implement every item below. Each lists the
exact current location and the desired end state. Aim for pixel-level polish —
the doctor-facing triage view must look clean, clinical, and trustworthy.

## Context: how a triage node is produced today
- The server endpoint `get_triage_timeline` (`backend/main.py:2501`) aggregates
  four sources into one `timeline` array: `initial`, `preop`, `intraop`,
  `postop`. Each node has `at`, `phase`, `phase_label`, `tier_before`,
  `tier_after`, `changed`, `triggered_by`, `source`, `reasons[]` (each reason:
  `{kind, code, label, detail?, weight?}`).
- The front end renders this in `renderTriageModal()` /
  `renderTimeline()` (`frontend/doctor.html:3710`–`3815`), node markup at
  `:3782`–`3801`. Helper `prettyTrigger()` is at `:3670`. Escalation-log list is
  rendered in `loadEscalations()` (`:3874`–`3918`).

---

## 1. Delete the "View Conversation" button (two places)

- **Escalation Log list footer** — `frontend/doctor.html:3914`, the
  `<button class="esc-link-btn" … data-chat-id=…>View Conversation</button>` inside
  `.esc-footer`. Remove the button. Also remove its now-dead click handler
  (`byId("escalationList").querySelectorAll("[data-chat-id]")…` around `:3920`).
  Keep the "Escalation Resolved Y/N" controls in the footer.
- **"View Triage" modal** — `frontend/doctor.html:3751`, the
  `<button … data-modal-chat-id=…>View Conversation</button>` inside
  `.triage-actions`. Remove it and its handler
  (`shell.querySelector("[data-modal-chat-id]")…` at `:3817`). Keep the
  "Send Intervention" button + composer intact.
- You may leave the helper `openEscalationConversation()` (`:3679`) defined but
  unused, or delete it if nothing else references it (verify with a search).

## 2. Redesign the "Risk Timeline" inside "View Triage"

Reference design (from the user's sample): each event is a clean card with a
clear date, a humanized event title, a strong phase label, and a tier transition
chip on the right — **no internal scoring jargon**. Edit
`renderTimeline()` node markup (`frontend/doctor.html:3782`–`3801`) and the
related CSS (`.triage-node*`, `.triage-reason*`, `.triage-tag*` at `:603`–`645`).

### 2a. Humanize the event title / trigger typography
Today the node shows the raw `triggered_by`/`source` string verbatim via
`prettyTrigger()` (e.g. `INITIAL_ASSESSMENT`, `demo:seed-intraop`,
`demo:day7-plus-checkins`). Replace these machine codes with clean,
clinician-readable titles. Extend `prettyTrigger()` (or add a
`TRIAGE_EVENT_LABELS` map) so that:
- `INITIAL_ASSESSMENT` → **"Initial assessment"**
- any `demo:seed-intraop` / intra-op source → **"Intra-op procedure event"**
- any `demo:day7-plus-checkins` / Day-N survey source → **"Day 7 check-in
  survey"** (derive the day number when available)
- Strip any `demo:` prefix and convert `snake_case`/`SCREAMING_CASE` to Sentence
  case as a safe fallback.
Style this title with stronger typography (slightly larger, `font-weight:600`,
`color:#0f172a`) — it is the headline of the card, not muted metadata.

### 2b. Reason box typography + design
The reason label (e.g. "Hip/femur fracture base risk", "Intra-op event: BP
instability requiring vasopressors", "Patient scored RED on Day 7 survey") is
the clinically meaningful line. Make `.triage-reason` (`:622`) read as a
well-designed callout: comfortable padding, readable size (~13px), clear text
color, a soft left accent that reflects severity (red accent when the node is a
TIER_3 / hard escalation, neutral otherwise), and keep the existing `detail`
sub-line muted beneath it. The box should feel intentional, not like a debug dump.

### 2c. Remove the BASE / SOFT / HARD / "W n" markers
Delete the `.triage-reason-tags` block entirely from the node markup
(`frontend/doctor.html:3795`–`3798`) — both the `kind` tag
(`BASE`/`SOFT`/`HARD`/`INFO`) and the `w {weight}` tag. Doctors must not see the
internal contributor kind or weights (that logic stays admin-only). You may keep
using `kind === "HARD"` purely to drive the **red accent styling** of the reason
box, but render no visible kind/weight chips. Remove the now-unused `.triage-tag`
CSS if nothing else uses it (search first).

### 2d. Keep the rest of the node intact
- Keep the date (`fmt(node.at)`), the **phase label** (`.triage-node-phase`), and
  the tier-transition chip (`formatTierTransition`, `:3697`) — these are good.
  Just make sure they sit in a clean, aligned header row after the redesign.
- Keep the "Show all assessments / Show tier changes only" toggle (`:3737`,
  `:3807`).

## 3. FIX THE PHASE BUG (Pre-Op vs Post-Op) — architectural correctness

**Bug:** a Day-7 post-op survey escalation is labeled **"Pre-Op"** in the Risk
Timeline (see the `TIER 2 → TIER 3` "Patient scored RED on Day 7 survey" node).
It must read **"Post-Op"**.

**Root cause** is in `classify_phase()` inside `get_triage_timeline`
(`backend/main.py:2525`–`2564`). It prioritizes surgery-timestamp windowing over
the event's authoritative `source`. The demo patients don't reliably have
`or_started_at` / `or_ended_at` / `discharge_at` set, so when `or_started_dt is
None` (or the event `at` predates it), line `:2543`–`2544` returns `PRE_OP`
**even for `source == "postop"` events** — which are post-op by definition.

**Fix — make `source` authoritative for phase**, then use timestamps only to
refine the day number:
- `source == "postop"` ⇒ always `POST_OP`. Use `days_since_discharge` (or the
  discharge-relative timestamp) only to compute the `Post-Op — Day N` suffix;
  default to `"Post-Op"` when the day is unknown.
- `source == "intraop"` ⇒ always the intra-op phase (`After Intra-Op Procedure`
  / `Intra-Op (in OR)`), never Pre-Op/Post-Op.
- `source in ("preop", "initial")` ⇒ `PRE_OP` (timestamp windowing may still
  promote `initial` to intra/post if real surgery timestamps exist, but a preop
  retier event is Pre-Op).
Restructure `classify_phase` so the `src`-based decision happens **before** the
`or_started_dt is None` fallthrough, eliminating the path where a `postop` event
can ever be labeled Pre-Op. This must hold for **all patients**, not just the
demo seed.

**Secondary data fix (so Day-N renders):** the seed at
`backend/triage_demo_seed.py:858`–`879` saves the Day-7 post-op retier event with
`inputs_snapshot={"day": 7}`, but `classify_phase` reads
`inputs_snapshot.get("days_since_discharge")`. Reconcile this — either add
`days_since_discharge` to the seeded snapshot or have `classify_phase` also honor
a `day` key — so the node can show "Post-Op — Day 7".

**Tests:** update/extend `backend/tests/test_triage_timeline.py` (the existing
`test_triage_timeline_aggregates_sources_and_labels_phases` already asserts the
postop node's `phase_label` starts with "Post-Op"). Add an explicit regression
test: a `postop`-source node whose patient has **no surgery timestamps** must
still classify as `POST_OP` (this is the exact bug). Keep `cd backend &&
python3 -m pytest tests/ -q` green.

## 4. Replace "Escalation Context" with a "Most recent triage change" highlight

In `renderTriageModal()` remove the **Escalation Context** panel entirely
(`frontend/doctor.html:3741`–`3746` — the Origin / Trigger / Consent block).

In its place, add a prominent **"Most recent triage change"** highlight panel
(reuse `.triage-panel` styling, but visually emphasized — e.g. accent border
matching the current tier color). It should summarize the latest *changed* node
in the timeline. Compute it from the same `timeline` data already in scope: take
the most recent node where `changed === true` (timeline is sorted oldest→newest,
so the last changed node). Show:
- the humanized event title (from 2a, e.g. "Day 7 check-in survey"),
- the corrected **phase label** (e.g. "Post-Op — Day 7"),
- the tier transition chip (e.g. `TIER 2 → TIER 3` via `formatTierTransition`),
- the primary reason label (e.g. "Patient scored RED on Day 7 survey.").
For the Sandra/Linda example this must read as the **post-op Tier 2 → Tier 3**
change driven by the Day-7 RED survey — never Pre-Op. If there is no changed
node, fall back to the current tier line.

## 5. Consistency / "reflect the backend triage logic faithfully"
- The whole point: the phase shown in the UI must be derived from the same
  source-of-truth the backend uses to compute tiers (the per-source retier
  events), not from brittle timestamp heuristics. After the §3 fix, confirm
  every node's phase matches its `source` for all patients in the demo
  (`initial`/`preop` → Pre-Op, `intraop` → intra-op, `postop` → Post-Op).
- Verify visually for multiple seeded patients (not just one) that:
  the "View Conversation" buttons are gone, the Risk Timeline reason boxes have
  no BASE/SOFT/HARD/W-n chips, titles are humanized, Post-Op events never show
  Pre-Op, and the "Most recent triage change" highlight is correct.

## 6. Validation
- `cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload`
- Open the doctor portal → Escalation Log → confirm no "View Conversation"
  button. Click **View Triage** on a post-op escalation (e.g. the Day-7 RED
  patient) → confirm: no "View Conversation", no Escalation Context, redesigned
  Risk Timeline with humanized titles + clean reason boxes + no kind/weight
  chips, correct **Post-Op** phase, and the "Most recent triage change" highlight.
- `cd backend && python3 -m pytest tests/ -q` — all green, including the new
  post-op-phase regression test.
- Keep edits confined to `frontend/doctor.html`, `backend/main.py`,
  `backend/triage_demo_seed.py`, and `backend/tests/test_triage_timeline.py`.
