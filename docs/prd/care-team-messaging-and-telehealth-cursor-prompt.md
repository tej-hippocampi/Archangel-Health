# Cursor Build Prompt — Care-Team ↔ Patient Threaded Messaging + Embedded Telehealth

> **How to use this doc:** Paste this into Cursor as the implementation brief. It is written
> against the **real** Archangel-Health codebase (FastAPI + static HTML/JS), not the
> aspirational Next.js/Prisma stack described in the source PRD. Where the PRD and the codebase
> disagree, **the codebase wins** — this doc has already done that translation for you.
>
> Two features ship here:
> - **Part 1 — Care-Team ↔ Patient Threaded Messaging** (the intervention redesign).
> - **Part 2 — Embedded Telehealth Video Conferencing** (PRD §10 / Feature 5, re-positioned for this stack).

---

## 0. Repo orientation (read this first)

CareGuide / Archangel-Health is a **single-service Python FastAPI app** that serves a **static
HTML/CSS/JS frontend**. There is **no Next.js, no Prisma, no Postgres, and no build step** for the
main app. The source PRD assumes that stack — **ignore that part of the PRD**. The actual ground truth:

| Concern | Reality in this repo |
|---|---|
| Backend | `backend/main.py` (~5.4k lines) + routers in `backend/routers/*.py`, FastAPI |
| Persistence | `backend/team_store.py` — SQLite (`TEAM_DB_PATH`, default `backend/team.db`) for episodes/events/escalations; **in-memory** `_patient_store` dict for the patient roster (resets on restart unless `DEMO_PERSIST_PATIENT_STORE=1`) |
| Patient dashboard | `GET /patient/{id}` serves `frontend/index.html`, injecting `window.__PATIENT__`. JS lives in `frontend/app.js` + `frontend/postop.js`. Styles in `frontend/styles.css` |
| Doctor portal | `GET /doctor/app` serves `frontend/doctor.html` (single 5.5k-line file, inline `<script>`). Auth = tenant staff JWT in `localStorage["archangel_doctor_auth_token"]`, attached by `apiJson()` |
| Patient identity | **No login.** Patients resolve via `clinic_code` + `resource_code` → `GET /api/patient/by-codes` → redirect to `/patient/{id}`. Codes live on the patient record |
| Staff identity | `backend/staff_context.py` → `StaffContext{ email, name, role, tenant_id, ... }`. Roles: `surgeon`, `rn_coordinator`, `np_pa`, `system_admin`. Resolve via `Depends(get_staff_context_optional)` |
| Email | `backend/email_utils.py` (`_send_html_email_with_reason_impl`, `is_email_transport_configured`). SendGrid Web API, SMTP fallback |
| Video today | **None human-to-human.** `backend/integrations/tavus.py` powers the AI **avatar** (Digital Care Companion) only. Tavus runs on Daily under the hood, which is convenient for Part 2 |
| Run it | `cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload` |
| Tests | `cd backend && python3 -m pytest tests/ -q` (pytest). Add tests next to the existing ones |

**Conventions to match (do not invent new patterns):**
- New persisted state → add a table in `team_store._init_schema()` (look at `escalations`, `event_logs`)
  and expose typed methods on `TeamStore` (e.g. `create_escalation`, `log_event`, `get_events`).
- Use `team_store.log_event(...)` for audit breadcrumbs on every state change.
- Backend HTML pages are built inline in `main.py` or served from `frontend/*.html`; follow whichever
  the neighboring route uses.
- Frontend: vanilla JS, no framework. Doctor UI uses `apiJson(path, opts)` which auto-attaches the Bearer
  token. Patient UI uses bare `fetch` (no auth header) — identity is implied by the `patient_id` in the URL.
- CSS design tokens: patient side `frontend/styles.css` (`--primary #2563EB`, `--radius`, etc.);
  doctor side tokens are defined at the top of `doctor.html` (`--accent #2563eb`, `--line`, tier-badge colors).

**Current intervention flow (the thing we are changing):**
- Backend: `POST /api/escalations/{escalation_id}/intervention` in `main.py` (~line 2677). Today it builds an
  HTML email containing the **full message text** and emails it to the patient, then logs a
  `provider_intervention_email` event. The email subject is `f"{_provider_email_signature(staff)} — URGENT CARE MESSAGE"`,
  and `_provider_email_signature` (main.py ~line 213) already produces strings like
  **"Maria Castillo, RN Coordinator, Archangel Triage Demo Clinic"**.
- Frontend (doctor): the "Send Intervention" composer lives inside the triage modal `renderTriageModal()`
  in `doctor.html` (~line 3710–3855). One-shot textarea → `POST .../intervention`. No history, no replies.
- There is **no patient-facing surface** for these messages today.

---

# PART 1 — Care-Team ↔ Patient Threaded Messaging

## 1.1 What changes (plain English)

Replace the fire-and-forget intervention email with a **persistent, two-way, per-patient message thread**:

1. A clinician sends a message to the patient from the **"view triage"** detail view.
2. The patient gets an **email notification** that says, in effect, *"Maria Castillo, RN Coordinator,
   Archangel Triage Demo Clinic has sent you a message,"* and instructs them to **enter their health
   system code and resource code** (exactly like every other patient email) to read it. **The message
   body itself is NOT in the email** — only the notification + the code-entry instruction.
3. In the patient dashboard **banner** (the row with the "Pre Operation / Post Operation" tabs and the
   "💬 Any further questions?" pill), add a **"View Care Team Message" button in the right corner**.
4. Clicking it opens an **inline page / overlay** where the patient reads care-team messages, each
   **labeled by sender role** (e.g. *"From your TEAM Surgeon — Dr. Thompson"* vs *"From your RN Care
   Coordinator — Maria Castillo, RN"*), and can **reply**. Replies route back to the specific clinician/role
   that sent the message.
5. Back in the doctor portal "view triage" view, add an **"Intervention Messages & History"** section
   showing the full thread for that patient, where the clinician can **reply**. **Replies from the
   clinician trigger a patient email notification; replies from the patient do NOT email the clinician**
   — clinicians see new patient replies in-app only. (Only patients are alerted by email.)

> **Scope of a thread:** per **patient**, not per escalation. Surfacing happens from the triage/escalation
> detail view, but the thread is keyed by `patient_id` so the same conversation is visible from any
> escalation for that patient and from the patient dashboard. Each message records the sender's role so the
> UI can label it and so replies can target the right recipient.

## 1.2 Data model (add to `team_store.py`)

Add one table in `_init_schema()` and typed methods on `TeamStore`.

```sql
CREATE TABLE IF NOT EXISTS care_team_messages (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id        TEXT    NOT NULL,
  escalation_id     INTEGER,                 -- nullable; the escalation it was sent from, if any
  health_system_id  TEXT,                    -- tenant scoping, mirror escalations table
  sender_type       TEXT    NOT NULL,        -- 'CARE_TEAM' | 'PATIENT'
  sender_role       TEXT,                    -- 'surgeon' | 'rn_coordinator' | 'np_pa' | NULL for patient
  sender_name       TEXT,                    -- e.g. 'Maria Castillo' (display)
  sender_email      TEXT,                    -- staff email; identifies the exact clinician for reply targeting
  recipient_role    TEXT,                    -- for a patient reply: the role/clinician it is directed to
  recipient_email   TEXT,                    -- the clinician a patient reply is addressed to
  body              TEXT    NOT NULL,
  read_by_patient   INTEGER DEFAULT 0,
  read_by_care_team INTEGER DEFAULT 0,
  created_at        TEXT    NOT NULL
);
```

Methods to add (mirror the style of `create_escalation` / `list_escalations` / `get_events`):

```python
def create_care_team_message(self, *, patient_id, sender_type, body,
                             escalation_id=None, health_system_id=None,
                             sender_role=None, sender_name=None, sender_email=None,
                             recipient_role=None, recipient_email=None,
                             created_at=None) -> int: ...

def list_care_team_messages(self, patient_id: str) -> list[dict]:  # ASC by created_at
    ...

def mark_care_team_thread_read(self, patient_id: str, *, by: str) -> None:
    # by == 'patient'  -> set read_by_patient=1 for CARE_TEAM messages
    # by == 'care_team'-> set read_by_care_team=1 for PATIENT messages
    ...

def count_unread_for_patient(self, patient_id: str) -> int:   # CARE_TEAM msgs, read_by_patient=0
    ...
def count_unread_for_care_team(self, patient_id: str) -> int: # PATIENT msgs, read_by_care_team=0
    ...
```

> Reuse the existing tenant-scoping convention (`health_system_id`) so a clinician only ever sees threads
> for patients in their tenant. The `escalations` table migration is your reference.

## 1.3 Backend endpoints

Add to `main.py` (or a small new `backend/routers/messaging.py` mounted in `main.py` — match how
existing routers are included). Helpers already exist: `_assert_clinical_staff_can_access_patient`,
`_require_clinical_staff`, `_provider_email_signature`, `_provider_role_display`,
`is_email_transport_configured`, `_send_html_email_with_reason_impl`, `_patient_store`, `_team_store`.

### Clinician side (auth: `StaffContext` required)

```
POST /api/patients/{patient_id}/care-team-messages
  body: { message: str, escalation_id?: int }
  - require clinical staff + patient access scope (reuse _assert_clinical_staff_can_access_patient)
  - persist a CARE_TEAM message: sender_role = staff.role, sender_name = staff.name,
    sender_email = staff.email, recipient is the patient.
  - send the PATIENT EMAIL NOTIFICATION (see 1.4). If email transport is unconfigured OR patient has no
    email, still persist the message but return { ok: true, emailed: false, reason } so the in-app thread
    works even when SendGrid is off. (Do NOT 5xx the whole send just because email is down — that's a
    regression risk; surface emailed=false instead.)
  - team_store.log_event(patient_id, 'care_team_message_sent', {message_id, sender_role, emailed})
  - returns { ok, message_id, emailed }

GET  /api/patients/{patient_id}/care-team-messages
  - require clinical staff + scope
  - returns { messages: [...], unread_from_patient: int }
  - side effect: mark_care_team_thread_read(by='care_team')
```

### Patient side (NO auth header — identity via codes, like the rest of the patient app)

```
GET  /api/patient/{patient_id}/care-team-messages
  - no Bearer required; patient_id in path is the capability (consistent with /patient/{id} dashboard)
  - returns { messages: [...] }  with each message shaped for display:
      { id, direction: 'incoming'|'outgoing', sender_label, sender_role, body, created_at, read }
    where sender_label for CARE_TEAM = f"{sender_name}{', ' + role_display}" e.g. "Maria Castillo, RN Coordinator"
  - side effect: mark_care_team_thread_read(by='patient'); log_event 'care_team_message_viewed'

POST /api/patient/{patient_id}/care-team-messages/reply
  body: { message: str, in_reply_to?: int }
  - persist a PATIENT message. **The patient explicitly picks who they are replying to** — the reply UI
    presents the distinct clinicians/roles who have messaged this patient (e.g. "Reply to your Surgeon —
    Dr. Thompson" vs "Reply to your RN Coordinator — Maria Castillo"). The chosen recipient sets
    recipient_role/recipient_email. If only one clinician is in the thread, preselect them. `in_reply_to`
    may also pin the target to a specific prior message.
  - **DO NOT send any email.** Clinicians are alerted in-app only.
  - team_store.log_event(patient_id, 'patient_care_team_reply', {message_id, recipient_role})
  - returns { ok, message_id }
```

> **Keep the old route working.** `POST /api/escalations/{id}/intervention` should now delegate to the
> same persistence + notification path (create a `CARE_TEAM` message with `escalation_id` set) so the
> existing doctor composer and `test_intervention_email.py` keep passing. The new behavior = persist +
> notify-only email, instead of emailing the full body. Update that test's expectations accordingly
> (assert the email is a notification, not the verbatim message body) and add new tests for the thread
> endpoints.

## 1.4 The patient email (notification-only)

Rewrite the email built in the intervention path. **The body must NOT contain the message text.** It must:

- Greet the patient by name.
- State who sent it, using the existing signature helper, e.g.
  *"**Maria Castillo, RN Coordinator, Archangel Triage Demo Clinic** has sent you a secure message
  through Archangel Health."*
- Instruct them to **enter their Health System Code and Resource Code** to view it — same language/pattern
  the platform already uses elsewhere (see the code-entry email blocks in `main.py` around lines
  1025–1034 and 3223–3257, which already render "Health System Code" / "Resource Code"). Reuse that
  visual block so it looks identical to existing patient emails.
- Include the **link to the code-entry page** (`LANDING_URL` if set, else `BASE_URL`), exactly as other
  patient emails do. After entering codes the patient lands on `/patient/{id}` where the new
  **"View Care Team Message"** button is waiting.
- Keep `importance_headers=True` and the confidentiality footer.

Subject stays role-stamped, e.g. `f"{_provider_email_signature(staff)} — New secure message"` (drop the
all-caps "URGENT CARE MESSAGE" wording unless the message is escalation-flagged; your call, but keep it
honest — not every message is urgent).

## 1.5 Patient frontend — banner button + inline message page

**Banner button** — in `frontend/index.html`, the banner row is around lines 58–86 (the
`.episode-top-tabs` element + the `#questionsBtn` `.questions-trigger-btn` pill). Add a **"📩 View Care
Team Message"** button in the **right corner** of that row. Match the existing pill aesthetic but give it a
distinct color so it reads as care-team (suggest the success/teal family, not the purple chat gradient).
Show an **unread badge** (count dot) when there are unread care-team messages.

Wire it in `frontend/app.js` (the file already has `initQuestionsButton()`, `trackPatientEvent()`,
overlay helpers, and `PATIENT` from `window.__PATIENT__`):

- On load, `GET /api/patient/${PATIENT.id}/care-team-messages` to compute the unread badge (cheap; the
  dashboard already makes similar calls).
- On click, open an **inline overlay** (reuse the existing overlay pattern — there are `overlay-*`
  elements + `initOverlays()` in app.js; copy that structure, don't invent a modal system). The overlay:
  - Header: "Messages from your care team" + back/close.
  - A **thread list**: incoming messages left-aligned with a labeled sender chip
    (**"Maria Castillo, RN Coordinator"** / **"Dr. Thompson, Surgeon"**), outgoing (patient) replies
    right-aligned. Show timestamps. Reuse the chat bubble styles already in `styles.css`
    (`.assistant-message` / `.patient-message`).
  - A **reply composer** at the bottom: a **recipient picker** (the distinct clinicians/roles who have
    messaged this patient — "Surgeon — Dr. Thompson" / "RN Coordinator — Maria Castillo"; auto-selected
    when there's only one) + textarea + Send → `POST /api/patient/${id}/care-team-messages/reply` with the
    chosen recipient. On success, append the bubble optimistically and clear unread.
  - Mark-read happens server-side on GET; clear the badge after open.

> Accessibility: keyboard-focusable button, `aria-label`, the overlay traps focus like existing overlays.

## 1.6 Doctor frontend — "Intervention Messages & History" section

**Entry point — the "Send Intervention" button becomes a two-way chooser.** In `frontend/doctor.html`,
inside the triage detail view (`renderTriageModal()`, ~line 3710), the existing **"Send Intervention"**
action no longer opens the email composer directly. Clicking it now reveals **two choices**:
- **"Start Telehealth Visit"** → creates an encounter (`POST /api/telehealth/encounters`) and opens the
  clinician room (Part 2).
- **"Send Patient a Message"** → opens the message composer that posts to
  `POST /api/patients/{pid}/care-team-messages` (persist + notification email) and refreshes the history
  panel below.

Keep this lightweight (two buttons / a small inline menu using existing `.btn` styles) — don't introduce a
new modal framework.

Then, **below that chooser**, add a new **"Intervention Messages & History"** panel using the existing
`.triage-panel` / `.triage-composer` / `.btn`/`.btn.primary` classes:

- On modal open, `apiJson('/api/patients/${pid}/care-team-messages')` and render the full thread, newest
  last. Label each message: care-team messages show the sender role + name and a tier-styled chip;
  **patient replies are visually distinct** ("Patient — John Doe") and highlighted when unread.
- A **reply box** at the bottom of the panel → `POST /api/patients/${pid}/care-team-messages` (this both
  persists and emails the patient). After send, append to the thread and toast "Message sent — patient
  notified by email."
- The "Send Patient a Message" chooser action and this history reply box must call the **same JS helper**
  hitting the same endpoint — avoid two parallel code paths — so a sent message immediately appears in the
  thread.
- **No email is sent to the doctor for patient replies.** New patient replies surface **in the existing
  top-right notification bell** (`#doctorNotifBell`, fed by `/api/doctors/me/notifications`,
  `loadIntakeNotifications()` ~line 2977). Extend that feed so each unread patient reply appears as its own
  notification line that **names the patient**, e.g. *"New message from John Doe"*, and clicking it opens
  that patient's "view triage" Intervention Messages & History panel. Update the bell count to include these
  unread replies (`count_unread_for_care_team`). Poll on portal load / modal-open — no websockets.

**Targeting requirement (explicit in the ask):** if Dr. Thompson (Surgeon) messages John Doe and John Doe
replies, the reply's `recipient_role/recipient_email` resolve to Dr. Thompson, and the thread shown under
John Doe's "view triage" displays that exchange. Replying from that panel sends only to John Doe. A
different patient's thread is never mixed in (everything is keyed by `patient_id`, tenant-scoped).

## 1.7 Acceptance criteria (Part 1)

- **AC-1.1** Sending from "view triage" persists a `CARE_TEAM` message and emails the patient a
  **notification that does not contain the message text**, instructing them to enter health-system +
  resource codes. With email transport off, the message still persists and the response reports
  `emailed: false`.
- **AC-1.2** The patient dashboard banner shows a "View Care Team Message" button with an unread badge;
  opening it lists messages labeled by sender role and lets the patient reply.
- **AC-1.3** A patient reply persists and is visible in the doctor "Intervention Messages & History"
  panel for that patient — **and triggers no email to any clinician**.
- **AC-1.4** Threads are strictly per-patient and tenant-scoped: a clinician cannot read another tenant's
  threads; a patient only sees their own.
- **AC-1.5** Every send/reply/view writes a `team_store.log_event(...)` audit breadcrumb.
- **AC-1.6** `pytest tests/` passes, including an updated `test_intervention_email.py` (email is now a
  notification) and new tests covering: clinician send, patient reply (no email), read-state transitions,
  and cross-tenant isolation.

---

# PART 2 — Embedded Telehealth Video Conferencing

> Source: PRD §10 (Feature 5). The PRD is high-level and assumes Next.js/Prisma. Below is that feature
> **re-positioned for FastAPI + static JS + `team_store` (SQLite)**. Build to *this*, not to the PRD's
> stack notes.

## 2.1 Goal

A clinician (NP/PA/surgeon/RN) launches a video visit from the doctor portal; the patient joins from a
**no-install, no-login magic link** (consistent with the codes-based patient model); the call captures
the data needed for a **TEAM-compliant claim**; and on call-end the system **auto-builds a draft claim**
in a billing queue. Loads fast, patient side is dead simple.

## 2.2 Vendor strategy — `VideoProvider` abstraction (Python)

Create `backend/integrations/video/` with a provider interface and a Daily.co implementation. Daily is the
default because it's HIPAA-eligible (BAA), sub-second joins, and **Tavus already runs on Daily**, so the
account/infra story is familiar. Keep the surface tiny so swapping to Twilio Video / Zoom for Healthcare /
Doxy.me is a <200-LOC change.

```python
# backend/integrations/video/base.py
from typing import Protocol, Optional
from dataclasses import dataclass

@dataclass
class VideoSession:
    provider: str
    session_id: str          # provider room id / name
    room_url: str            # base join url
@dataclass
class JoinToken:
    token: str
    join_url: str            # room_url + token, ready to open

class VideoProvider(Protocol):
    name: str
    async def create_session(self, *, encounter_id: str, max_minutes: int = 60,
                             record: bool = False) -> VideoSession: ...
    async def issue_token(self, *, session: VideoSession, display_name: str,
                          is_owner: bool, minutes_valid: int = 120) -> JoinToken: ...
    async def end_session(self, *, session_id: str) -> None: ...
```

```python
# backend/integrations/video/daily.py  -> class DailyVideoProvider implements VideoProvider
#   - POST https://api.daily.co/v1/rooms  (private, exp, eject_at_room_exp)
#   - POST https://api.daily.co/v1/meeting-tokens  (room_name, user_name, is_owner, exp)
#   - Auth: Authorization: Bearer ${DAILY_API_KEY}
#   - Graceful no-key fallback like tavus.py: if DAILY_API_KEY unset, return a stub session that
#     points at a local "video unavailable / configure DAILY_API_KEY" page so the UI still renders.
```

Add to `.env.example`: `DAILY_API_KEY=`, `DAILY_DOMAIN=` (e.g. `yourco.daily.co`),
`VIDEO_PROVIDER=daily`. Mirror the optional-key, graceful-degradation pattern in `tavus.py`
(print a `[video]` notice and return a stub rather than crashing) so the app still boots without a key.

> Client embed: use the Daily **prebuilt** iframe (`@daily-co/daily-js` via CDN `<script>` — no bundler,
> consistent with this repo's no-build frontend) on both the clinician and patient pages. Do not hand-roll
> WebRTC (explicitly out of scope in the PRD).

## 2.3 Data model (add to `team_store.py`)

Two tables, same migration/method pattern as Part 1.

```sql
CREATE TABLE IF NOT EXISTS telehealth_encounters (
  id                   TEXT PRIMARY KEY,           -- uuid
  patient_id           TEXT NOT NULL,
  health_system_id     TEXT,
  escalation_id        INTEGER,                    -- nullable: scheduled from an escalation/triage
  scheduled_for        TEXT,
  scheduled_clinician  TEXT,                       -- staff email
  clinician_role       TEXT,
  provider             TEXT,                       -- 'daily'
  vendor_session_id    TEXT,
  room_url             TEXT,
  patient_location     TEXT,                       -- 'HOME' (POS_10) | 'FACILITY_OTHER' (POS_02)
  patient_type         TEXT,                       -- 'NEW' | 'ESTABLISHED'
  started_at           TEXT,
  ended_at             TEXT,
  duration_seconds     INTEGER,                    -- cumulative; pauses on drop, resumes on rejoin
  hcpcs_code           TEXT,
  l45_attestation_json TEXT,                       -- { type, note } or NULL
  documentation_json   TEXT,                       -- SOAP-ish note draft
  status               TEXT NOT NULL,              -- SCHEDULED|WAITING|IN_PROGRESS|COMPLETED|NO_SHOW|REDIRECTED_TO_ED|CANCELLED
  claim_id             TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telehealth_claims (
  id                TEXT PRIMARY KEY,              -- uuid
  encounter_id      TEXT NOT NULL UNIQUE,
  patient_id        TEXT NOT NULL,
  health_system_id  TEXT,
  hcpcs_code        TEXT NOT NULL,
  pos               TEXT NOT NULL,                 -- '02' | '10'
  type_of_bill      TEXT NOT NULL,                 -- '13X'
  revenue_code      TEXT NOT NULL,                 -- '0780'
  demo_code         TEXT NOT NULL,                 -- 'A9'
  ride_alone        INTEGER NOT NULL DEFAULT 1,    -- always 1 for TEAM telehealth
  duration_minutes  INTEGER,
  status            TEXT NOT NULL,                 -- DRAFT_READY_FOR_REVIEW|SUBMITTED|PAID|DENIED
  audit_trail_json  TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
```

Expose `TeamStore` methods: `create_encounter`, `get_encounter`, `update_encounter`,
`list_encounters(patient_id=None, status=None, health_system_id=None)`, `create_claim`, `get_claim`,
`list_claims(...)`, `update_claim_status`.

## 2.4 G-code ladder (deterministic) — put in a small pure module

`backend/telehealth/gcodes.py` (pure function, easy to unit-test). Mirror PRD §10.7 exactly:

| Code | Patient type | Threshold |
|---|---|---|
| G0660 / G0661 / G0662 / G0663 / G0664 | New | ≥10 / ≥20 / ≥30 / ≥45 / ≥60 min |
| G0665 / G0666 / G0667 / G0668 | Established | ≥10 / ≥15 / ≥25 / ≥40 min |

```python
def map_gcode(patient_type: str, duration_minutes: int) -> str | None: ...
def next_threshold(patient_type: str, duration_minutes: int) -> tuple[str, int] | None: ...  # for the ladder UI
def requires_l45_gate(hcpcs: str) -> bool:  # True for G0663, G0664, G0668
```

POS mapping is deterministic from `patient_location`: `HOME -> '10'`, `FACILITY_OTHER -> '02'`.
`type_of_bill='13X'`, `revenue_code='0780'`, `demo_code='A9'`, `ride_alone=True` are **pre-stamped
constants** — define them once and have the claim builder enforce them at the schema level
(reject any attempt to attach a second/FFS line item with error `RIDE_ALONE_VIOLATION`).

## 2.5 Backend endpoints

Add `backend/routers/telehealth.py` (mount in `main.py`). Clinician routes require `StaffContext`; the
patient join route is **token-gated, not login-gated**.

```
POST /api/telehealth/encounters
  (staff) body: { patient_id, scheduled_for?, escalation_id? }
  - scope-check patient. Create encounter status=SCHEDULED. patient_type starts NULL; the clinician sets it
    on the pre-visit setup page (see below) — we surface a suggested default (derived from prior
    encounters / episode age) but the clinician confirms NEW vs ESTABLISHED explicitly.
  - **eligibility gate (AC-5.5):** if the parent episode's TEAM eligibility verdict is INELIGIBLE, block
    creation with a clear 409. (Eligibility lives in backend/eligibility + team_store; reuse it.)
  - returns { encounter_id }

POST /api/telehealth/encounters/{id}/session
  (staff) - lazily create the VideoProvider session + clinician owner token; persist vendor_session_id,
           room_url. returns { join_url } for the clinician.

POST /api/telehealth/encounters/{id}/invite
  (staff) - send the patient an SMS (twilio_client) + email with a magic join link:
           /telehealth/join/{id}?t={signed_token}. Reuse email_utils + integrations/twilio_client.
           Token = short-lived signed JWT bound to encounter_id (reuse tenant_jwt/auth signing helper).

GET  /telehealth/join/{id}?t=...           -> patient join page (HTML), no login. Verifies token.
GET  /telehealth/room/{id}                  -> clinician in-call page (HTML), staff-gated.

POST /api/telehealth/encounters/{id}/start  (staff) status=IN_PROGRESS, started_at=now (timer source of truth is server)
POST /api/telehealth/encounters/{id}/location (staff) body:{ location:'HOME'|'FACILITY_OTHER' } -> drives POS
POST /api/telehealth/encounters/{id}/heartbeat (both) optional: accumulate connected time for accurate duration
POST /api/telehealth/encounters/{id}/end    (staff) body:{ duration_seconds, outcome?:'COMPLETED'|'NO_SHOW'|'REDIRECTED_TO_ED' }
   - compute duration_minutes, map_gcode(patient_type, minutes); if requires_l45_gate -> status stays
     COMPLETED but claim build is blocked until attestation posted.
POST /api/telehealth/encounters/{id}/attest (staff) body:{ type:'STAFF_ONSITE'|'STAFF_NOT_REQUIRED', note }
POST /api/telehealth/encounters/{id}/build-claim (staff)
   - requires: ended + (l45 attestation if gated). Build DRAFT_READY_FOR_REVIEW claim with all pre-stamped
     TEAM attributes. Enforce ride-alone. No claim for NO_SHOW / REDIRECTED_TO_ED. Returns { claim_id }.
GET  /api/telehealth/claims/{id}/download (staff)
   - **the doctor-facing deliverable.** Returns the draft claim as a downloadable file with
     Content-Disposition: attachment (filename e.g. `claim_{patient_last}_{serviceDate}_{hcpcs}.pdf`).
     Generate a simple one-page PDF (or, if no PDF lib is wired, a clean human-readable .txt/.json) listing
     every claim field: patient, service date, HCPCS, POS, type-of-bill 13X, revenue 0780, demo A9,
     ride-alone, duration, L4/L5 attestation, and the audit trail. No external billing-queue workflow is
     required — the doctor just downloads the draft. Log a `claim_downloaded` event.
```

Every transition writes `team_store.log_event(...)` and appends to the claim `audit_trail_json`
(actor, action, at).

## 2.6 Frontend

**Clinician launch point:** this is the **"Start Telehealth Visit"** branch of the two-way chooser added in
Part 1 §1.6 (the former "Send Intervention" button → choose **"Start Telehealth Visit"** or **"Send
Patient a Message"**). It calls `POST /api/telehealth/encounters` and then routes the clinician to an
**inline pre-visit setup page** (not straight into the call).

**Pre-visit setup page** (inline, before the room): the clinician confirms a couple of billing-relevant
facts before the call starts:
- **Patient type — NEW vs ESTABLISHED** (a clear two-option selector; preselect the system's suggested
  default but require the clinician to confirm). This drives the entire G-code ladder.
- A quick visit reason (optional) and a "Send patient the join link" action (`/invite`).
- A **"Begin visit"** button → opens `/telehealth/room/{id}`.
Persist the chosen `patient_type` to the encounter (`PATCH`/dedicated endpoint) before the room opens.

**Clinician in-call page** (`/telehealth/room/{id}` → an HTML page; build it like the other served pages):
- Daily prebuilt iframe (clinician = owner token).
- **Clinician-only sidebar**: patient one-pager (pull from existing patient summary / battlecard data),
  current tier, last few triage signals; a collapsible visit playbook; a documentation textarea that
  autosaves to `documentation_json`; a **location prompt** ("Is the patient at home?") that posts to
  `/location`.
- **Visit timer** visible to clinician only; **server time is the source of truth** (use started_at +
  heartbeat accumulation, not client wall-clock). Show the **G-code ladder** with the next threshold as
  information only — **never nudge upward** (TEAM inverts the incentive; shorter is often better).
- **End call** → if gated code, show the **L4/L5 attestation modal** (PRD §10.8 copy verbatim) and block
  finalize until one option + note is provided → build the draft claim → show the resulting G-code **and a
  prominent "Download draft claim" button** (`GET /api/telehealth/claims/{id}/download`). That download is
  the end state for the doctor — no separate billing-queue handoff to chase.

**Patient join page** (`/telehealth/join/{id}?t=...` → HTML, no login):
- Verify token, show a friendly waiting room with a 15s mic/cam check and a "join by phone" fallback line.
- Daily prebuilt iframe (patient = non-owner token, `display_name = patient first name`). Minimal controls
  (mic/cam/leave) + a single chat box. **No clinician sidebar.**
- Mobile-first; target join < 5s.

## 2.7 Claim auto-build & guardrails

On `build-claim`, construct the draft per PRD §10.9 with `pos` from location, `hcpcs_code` from the ladder,
and the pre-stamped `type_of_bill='13X'`, `revenue_code='0780'`, `demo_code='A9'`, `ride_alone=True`.
The claim builder must reject any second/FFS line item with `RIDE_ALONE_VIOLATION`. The claim is saved as
`DRAFT_READY_FOR_REVIEW` and surfaced to the doctor as a **downloadable file** at end-of-call
(`GET /api/telehealth/claims/{id}/download`) — that download is the deliverable.

## 2.8 Acceptance criteria (Part 2)

- **AC-2.1** Patient opens the SMS/email magic link and reaches the waiting room with no login/install.
- **AC-2.2** 17-min established-patient call → at End call a **draft claim is built with G0666 and offered
  as a download** (file contains all TEAM attributes + audit trail); ladder shown during the call.
- **AC-2.3** 47-min established call → **L4/L5 attestation required** before the claim leaves draft.
- **AC-2.4** Any FFS/second line item on a TEAM telehealth claim fails with `RIDE_ALONE_VIOLATION`.
- **AC-2.5** Starting a visit on an **INELIGIBLE** episode is blocked with explanation.
- **AC-2.6** `NO_SHOW` / `REDIRECTED_TO_ED` encounters build **no** claim.
- **AC-2.7** App boots and pages render with `DAILY_API_KEY` unset (graceful "configure video" stub),
  mirroring the Tavus no-key behavior.
- **AC-2.8** New pure-logic units (`gcodes.py`, POS mapping, ride-alone enforcement) covered by pytest.

---

## Build order (suggested)

1. **Part 1 backend** — `care_team_messages` table + methods, the 4 endpoints, rewire
   `/intervention` to persist + notification-only email. Update/extend tests. *(Ship-able alone.)*
2. **Part 1 frontend** — patient banner button + overlay; doctor history panel.
3. **Part 2 backend** — `VideoProvider` + Daily impl, encounter/claim tables, `gcodes.py`, router, tests.
4. **Part 2 frontend** — clinician room page + patient join page (Daily prebuilt iframe).

## Definition of done

- `cd backend && python3 -m pytest tests/ -q` is green.
- App boots with **no** optional keys set (SendGrid/Daily/Twilio off) and degrades gracefully.
- No secrets committed; new keys added to `.env.example` only.
- Audit breadcrumbs (`log_event`) on every state change; tenant scoping enforced on every staff route.

---

## Resolved decisions (locked)

1. **Thread scope** — **Per patient**, surfaced from the triage view. ✅
2. **Reply targeting** — **The patient explicitly picks the recipient** (Surgeon vs RN Coordinator, etc.)
   via a recipient picker; auto-selected when only one clinician is in the thread. ✅
3. **Telehealth entry** — From the triage detail view, the former "Send Intervention" button becomes a
   **two-way chooser: "Start Telehealth Visit" / "Send Patient a Message."** ✅
4. **Email subject tone** — **Softened to "New secure message"**; only escalation-flagged sends read as
   urgent. ✅

5. **Clinician notifications** — patient replies surface in the **existing top-right notification bell**,
   one line per reply, **naming the patient** ("New message from John Doe"); clicking opens that patient's
   Intervention Messages & History. ✅
6. **Patient type (NEW vs ESTABLISHED)** — the clinician **picks it on the inline pre-visit setup page**
   (system suggests a default; clinician confirms). No EHR/FHIR dependency. ✅
