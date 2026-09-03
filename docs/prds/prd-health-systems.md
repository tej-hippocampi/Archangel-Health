# PRD: Health systems live (group C)

The supply side of the Sep 1 founder meeting: a health system finds us, books a
call, signs one agreement, and then hears from us every time we need data. Most
of that path already exists in the working tree after the origin/main merge.
This PRD names what is left: data-request broadcasts, an admin view of the
inbound leads, one env-driven Calendly URL, partner-visible upload accounting,
and the glue gaps found while verifying the flow that is already there.

## Problem (from the meeting)

Health systems do not know what data they have or what we want, so the call
comes early: homepage, interest form at /partner, straight to book-a-call. Form
submissions must reach the founders' email AND survive as an admin-side record,
because every submission is an attestation about authority over de-identified
data and that is a legal audit trail. After acceptance the portal exists for
tracking, not discovery: multi-user access, secure upload, payout visibility,
and data-request notifications. The meeting's concrete example: we broadcast
"we need 100 nephrology cases" to every partner, several may upload, we
approve what we take. None of the broadcast machinery exists today, the lead
record is write-only, and the Calendly link is a hardcoded constant.

## Decisions

Locked with the founders:

* **One authorized signer binds the organization.** The DLA flow merged from
  origin/main stands as built (`backend/asclepius/dla.py`, `hs_states.py`,
  `hs_provisioning.py`, `pdf_render.py`, `docs/legal/DLA_v1.md`). Every member
  is notified and receives the countersigned PDF; nobody re-signs. The
  meeting's every-member-signs ask is overridden by counsel-backed design
  already in the tree.
* **First-come-first-serve is informal.** A broadcast is an invitation, not a
  lock. Multiple partners may upload against one request; the admin approves
  what fulfills it. Requests close by hand or when the admin marks them
  fulfilled. No claiming, no reservation state.
* **Payout accrual stays manual.** The partner sees upload and acceptance
  counts; pricing and the ledger entries behind `/hs/payouts` remain an
  operator decision. Nothing in this PRD computes money.

Made here, with rationale:

* **Broadcast delivery reuses the durable-outbox shape**, not a new mechanism.
  `task_notify_outbox` (`backend/asclepius/store.py:3059`) and
  `admin_notify_outbox` (`store.py:2909`, whose comment says outright: same
  shape so it drains in the same loop rather than adding a second thing that
  can silently stop) set the precedent. A new `hs_request_outbox` follows it
  and drains on the same tick (`main.py` `_start_asclepius_task_notify_loop`,
  ~line 6606). Up to N-partners-times-members emails never run inline in the
  admin's request, and a crash mid-send loses nothing.
* **The lead read endpoint lives with the lead write** (`backend/routers/
  leads.py`), admin-gated, because `lead_submissions` is a team-store table
  and the leads router already holds the team-store handle. The admin surface
  lands in the existing Data tab, Systems subview (`admin_health.js`), because
  a partner lead is the top of the same pipeline that view already shows.
* **The Calendly URL becomes one shared landing config value** read from
  `import.meta.env`, because the repo currently ships two different hardcoded
  Calendly accounts (`PartnerInterest.tsx:33`, `TeamCalculator.tsx:24`) and
  that is exactly the drift an env var exists to end.
* **Uploads may reference a request but never require one.** An optional
  `request_id` on both upload doors ties a partner's response to the broadcast
  for the admin's fulfillment view. Optional, because most uploads will
  predate or ignore any request and must not grow a new precondition.

## Requirements

Numbered, each testable.

**Data-request broadcasts**

1. An admin can create a data request with: title, specialty, case count,
   due date, free-text details. Creating it enqueues one outbox row per active
   portal member of every health system whose state passes
   `hs_states.can_upload` (ACTIVE, including the legacy NULL collapse).
2. Delivery is idempotent per (request, health system, recipient email): the
   sha256 idempotency-key convention of `task_notify._idempotency_key`
   (`task_notify.py:40`). Re-broadcasting a request enqueues nothing new.
3. Emails send from the shared drain loop, never inline in the admin request.
   A failed send marks the row failed with a reason and leaves the rest of the
   batch alone (per-row defensiveness, as `drain_outbox`, `task_notify.py:86`).
4. The portal lists open requests on a new `GET /hs/requests`, gated like
   `/hs/payouts` (session-scoped, no id in the path), visible only when the
   organization is ACTIVE. Each entry carries title, specialty, count, due
   date, details, and copy stating that several partners may upload and the
   Archangel team confirms what it accepts.
5. An admin can close a request with a reason (`fulfilled` or `withdrawn`).
   Closed requests leave the portal list immediately and stay queryable on the
   admin side.
6. Both upload doors (multipart `POST /hs/uploads` and the chunked declare
   `POST /hs/uploads/sessions`) accept an optional `request_id`; an unknown or
   closed id is a 400, absence changes nothing. The admin request detail view
   lists uploads carrying its id, per health system.
7. Suppressed states never hear a broadcast: `intake`, `submitted`,
   `approved_awaiting_dla`, `declined` organizations get no email and see no
   request list.

**Admin lead view**

8. `GET /api/leads/admin` (name final at implementation; admin auth via
   `asc_auth.require_admin`) returns `lead_submissions` rows newest-first with
   source, email, message, created_at, paged by `limit`/`before_id`. All four
   sources from `leads.py:24` appear; `health_system_partner` is filterable.
9. The Data tab's Systems subview renders a "Partner leads" card above the
   systems list showing the newest submissions with source chips, the
   message verbatim, and a mailto reply link. Read-only in this PR.
10. Nothing about the write path changes: honeypot, rate limit, store-first
    then email, and the 503 fallback in `leads.py:86-131` stay as they are.

**Calendly config**

11. The landing build reads the Calendly URL from `VITE_CALENDLY_URL` through
    one shared module (`landing/src/app/config.ts`), falling back to the
    current PartnerInterest constant so a build without the env var behaves
    exactly as today. `PartnerInterest.tsx` and `TeamCalculator.tsx` both
    consume it; no component keeps its own URL. Prefill behavior
    (`calendlyUrl(name, email)`, `PartnerInterest.tsx:36-48`) is unchanged.

**Verification of the merged flow, and its glue gaps**

12. The documented lifecycle (`docs/asclepius/HEALTH_SYSTEM_ONBOARDING.md`)
    holds in the working tree; the gaps below get fixed or explicitly waived
    in review:
    * `GET /hs/uploads` (`asclepius_provider.py:1088`) depended on bare
      `require_hs_portal` while both upload doors use
      `require_hs_surface(UPLOAD)`. A pending member gets a 200 and an empty
      list where every sibling surface answers 403. Align it to the surface
      dependency.
    * A member added while the organization sits in `approved_awaiting_dla`
      receives only the member-added credential email
      (`_notify_hs_members_added`, `asclepius_provider.py:2493`); the
      agreement email went out at approval time, before they existed. The
      portal state rail rescues them, but the email trail is one letter
      short. Fix: when the org state is AWAITING_DLA, the member-added email
      includes the ready-for-signature line and link.
    * `lead_submissions` is write-only end to end: `team_store.py` has
      `record_lead_submission` (line 2563) and no reader. Requirement 8 is
      the fix.
    * Two different hardcoded Calendly accounts ship today (see Decisions).
      Requirement 11 is the fix.
13. What was verified working and needs no change: `/hs/members` self-serve
    (GET/POST at `asclepius_provider.py:2412/2422`; 25-account cap, re-add of
    an active address neither mints nor rotates, new member inherits the
    inviter's `approval_status`, per-member credential letters); the state
    machine edges and the NULL-reads-as-ACTIVE legacy collapse
    (`hs_states.py:88-119`); sign-time hash verification and the append-only
    `signed_agreements` triggers; `_hs_upload_preconditions` applied by all
    upload doors (`asclepius_provider.py:1158`).

**Payout-accrual visibility**

14. `GET /hs/uploads` gains a `summary` block: counts per partner-facing
    status (received, processing, accepted, needs_attention) and total
    accepted bytes, computed from the same rows the list already maps through
    `_hs_upload_view` (`asclepius_provider.py:1031`). No new query surface.
15. The portal payouts page shows one accrual line derived from that summary:
    "N uploads accepted and awaiting pricing" when accepted uploads exceed
    payout ledger entries. Wording avoids promising amounts. The ledger and
    invoice views behind `/hs/payouts` (`asclepius_provider.py:1960`) are
    unchanged.

## What exists today

Verified against the working tree on `claude/practice-case-gate` after the
origin/main merge:

* The whole signup-to-active path: OTP signup, intake, admin approve/decline,
  DLA render/hash/sign, countersigned PDF, upload gates. Modules and line
  anchors: states and transitions `hs_states.py:46-100`; account minting
  `hs_provisioning.py:40`; agreement rendering and hashing `dla.py:44,
  174-198`; executed PDF `dla.py:285-333`; the partner routes throughout
  `routers/asclepius_provider.py` (signup 1528, intake 1823, members 2412,
  agreement 2540-2737, payouts 1960, uploads 889/1245).
* Lead capture: `POST /api/leads` (`routers/leads.py:86`) with four sources
  including `health_system_partner`, stored via
  `team_store.record_lead_submission` (`team_store.py:2563`) into
  `lead_submissions` (`team_store.py:167-176`: source, email, message,
  user_agent, client_ip, created_at) and emailed to `LEAD_NOTIFY_EMAIL`.
* Outbox precedent: `task_notify_outbox` schema `store.py:3059-3076`, enqueue
  and drain `task_notify.py:55-146`, drain loop `main.py:6606-6640`,
  `admin_notify_outbox` `store.py:2909`.
* Admin surface: the admin console's four tabs (`asclepius.js:75`), Data tab
  subnav Systems / Pipeline tools / Export (`asclepius.js:9655-9668`),
  health-systems section in its own file exposing `window.AdminHealthSection`
  (`admin_health.js:1242`).
* Landing: `PartnerInterest.tsx` form-then-Calendly with prefill; the
  hardcoded URL at line 33.

## Gaps and changes per file

* `backend/asclepius/store.py`: two additions inside `_init_schema`, both
  following the house pattern (idempotent `CREATE TABLE IF NOT EXISTS` in the
  schema block; new columns via `if "col" not in cols("table"): conn.execute(
  "ALTER TABLE ... ADD COLUMN ...")`, as at line 853).
  * `hs_data_requests`: `id TEXT PRIMARY KEY, title TEXT NOT NULL, specialty
    TEXT NOT NULL, case_count INTEGER NOT NULL, due_date TEXT, details TEXT,
    status TEXT NOT NULL DEFAULT 'open', created_by TEXT NOT NULL, created_at
    TEXT NOT NULL, closed_at TEXT, closed_reason TEXT` plus a status index.
  * `hs_request_outbox`: the exact `task_notify_outbox` column shape
    (idempotency_key UNIQUE, status, send_attempts, last_error, sent_at,
    created_at) with `request_id TEXT`, `hs_id TEXT`, `recipient_email TEXT`.
  * A nullable `request_id` column on the uploads table read by
    `list_uploads_for_health_system`, via the guarded ALTER pattern.
  * Store methods mirroring the task-notify set: create/list/close requests,
    enqueue (INSERT OR IGNORE on the idempotency key, as `store.py:4171`),
    list-pending, mark-sent, mark-failed.
* `backend/asclepius/hs_request_notify.py` (new): `enqueue_for_request` and
  `drain_outbox`, modeled line-for-line on `task_notify.py` including the
  never-raise discipline.
* `backend/main.py`: the notify loop drains the new outbox in the same tick.
* `backend/routers/asclepius_admin.py`: create / list / close / detail
  endpoints for requests, admin-gated.
* `backend/routers/asclepius_provider.py`: `GET /hs/requests`; optional
  `request_id` on both upload doors; the `summary` block on `GET /hs/uploads`;
  the surface-dependency fix on that route; the AWAITING_DLA line in the
  member-added path.
* `backend/routers/leads.py` + `backend/team_store.py`: the admin read
  endpoint and `list_lead_submissions`.
* `backend/onboarding_emails.py`: `build_hs_data_request_email` (plain
  what-we-need letter: specialty, count, date, portal link), and the
  conditional agreement line in `build_hs_member_added_email`.
* `frontend/asclepius/admin_health.js`: Partner-leads card; a Requests card
  (compose form, open list with close buttons, per-request upload tally).
  Stays inside this file's `window.AdminHealthSection`; `asclepius.js` is not
  touched beyond what already mounts it.
* `frontend/provider/`: the open-requests list on the portal home.
* `landing/src/app/config.ts` (new), `PartnerInterest.tsx`,
  `TeamCalculator.tsx`: the env-driven URL.

## Email and notification touchpoints

* New: the data-request letter, one per active portal member, via the outbox.
* Changed: member-added letter gains its conditional agreement line.
* Unchanged: lead notification to `LEAD_NOTIFY_EMAIL`, signup OTP, approval
  and agreement letters, countersigned-PDF distribution.
* No in-app community posts from this PRD; health-system partners are not
  community members (`community/router.py:266` scopes membership to
  physicians).

## Test plan

Repo style: plain pytest functions, outside-in through the HTTP surface,
each with a docstring saying WHY the case exists (see
`tests/test_hs_onboarding.py`). New file `tests/test_hs_data_requests.py`:

* Broadcast reaches every member of every ACTIVE org and nobody in any other
  state, including a legacy NULL-state org (the collapse is load-bearing).
* Re-broadcast enqueues zero new rows; the idempotency key is the arbiter.
* A failed transport marks the row failed and later drains retry nothing that
  was sent (crash-safety is the reason the outbox exists).
* Portal request list: visible to ACTIVE, absent for AWAITING_DLA, gone once
  closed; upload with a valid, unknown, and closed `request_id`.
* Admin close with each reason; detail view tallies tagged uploads.

Additions to existing files: `tests/test_hs_onboarding.py` gets the
`GET /hs/uploads` surface-gate case (a pending member is told 403, the same
answer every sibling surface gives) and the AWAITING_DLA member-added email
line; a new `tests/test_lead_admin.py` covers the read endpoint (admin-only,
newest-first, source filter, honeypot rows absent because they were never
stored). Landing change is covered by the fallback behavior being the current
constant; no landing test harness exists and none is introduced here.

## Out of scope

* Payout pricing, accrual computation, Stripe/ACH rails (group G).
* De-identification tooling for partners who cannot de-identify (explicitly
  deferred in the meeting).
* Calendly booking webhooks and auto calendar invites (the link with prefill
  stands; a webhook needs a Calendly app we do not have today).
* Any change to the one-signer DLA design or the legal text.
* The admin redesign (group F); everything here lands in the existing tabs.
* Request claiming, reservations, or per-partner quotas: FCFS stays informal.
