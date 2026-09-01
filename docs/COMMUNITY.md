# Asclepius Community — architecture & deployment

The Asclepius Community is the in-house, credential-gated, real-time messaging
workspace for contributor physicians (Community PRD). Functionally a Slack
clone; visually the Archangel console design system; built entirely in-house —
no third-party chat SDK. It is **additive**: nothing in the evaluation flow
(V1–V4) is modified.

## Where it lives

| Piece | Path |
|---|---|
| Backend package | `backend/community/` (`router.py`, `store.py`, `phi_gate.py`, `ws.py`, `notify.py`, `attachments.py`, `schema.py`) |
| HTTP + WS surface | `/api/community/*`, `WS /api/community/ws` (mounted in `main.py`) |
| Page shell | `GET /community` → `frontend/asclepius/community.html` |
| Frontend app | `frontend/asclepius/community.js` + `community.css` (vanilla SPA, console tokens) |
| Portal entry | "Community" item in the portal SIDE PANEL (`asclepius.js`), opens a new tab at `/community?t=<handoff>`; the rail badge polls `GET /community/unread` and `POST /community/handoff` mints the short-lived single-use handoff code (redeemed by the page via `POST /api/community/handoff/redeem`) |
| Persistence | Own SQLite DB (`COMMUNITY_DB_PATH`, default `backend/community.db`) — never touches `asclepius.db` or `team.db` |
| Tests | `backend/tests/test_community.py` |

## Access model (PRD §1)

There is **no** registration route, invite link, email join, or external SSO.
The only way in:

1. An authenticated Asclepius session (the same JWT the portal uses; the new
   tab reads it from same-origin localStorage).
2. Role `evaluator` **with a verified credential profile**
   (`contributor_credentials.credentials_verified`), or Archangel staff
   (`admin` / `qa_reviewer`). Buyers and data partners are refused everywhere.
3. Not community-banned (`community_bans` — moderation is community-scoped so
   the evaluation account is untouched).

The gate runs **server-side on every REST call and the WebSocket**, not just
at page load. Ineligible users get a clean "Community access is for verified
contributors." screen.

Unread endpoints, two tiers: `GET /community/unread` → `{total}` is the
portal-rail contract (soft — non-members get 0); `GET /api/community/badge`
is the detailed variant (`unread`/`mentions`/`dm_unread`/per-channel) kept as
API surface for richer clients and as the semantic anchor for the unread
tests. Both share one computation.

WebSocket auth: the client exchanges its JWT for a **single-use, 60s ticket**
(`POST /api/community/ws-ticket`) and connects with `?ticket=` — the long-lived
JWT never appears in a URL/access log.

## PHI blocking at send (PRD §7)

The highest-risk surface. Controls, all server-side and fail-closed:

* `phi_gate.scan_text` runs in the message **create and edit** handlers before
  persistence and before any broadcast. Categories: patient_name, mrn, ssn,
  dob, exact_date, phone, email, address, account_number (incl. Medicare MBI).
  Clinical content (lab values, doses, diagnoses, age bands) deliberately
  passes; identifier patterns require digits and plausible month/day pairs so
  "the payout policy changed" or "25-50-100 titration" never false-block.
* Blocked sends return `422` with **category + character span only** — the
  matched text is never echoed in the response, logs, or audit. The composer
  highlights the span and offers one-tap "Remove flagged text & post".
* Blocked text is **never stored** — no quarantine table, nothing.
* Every block event is written to the hash-chained audit log (categories only)
  and counted per user; `COMMUNITY_BLOCK_FLAG_THRESHOLD` (default 3) raises an
  admin flag (`GET /api/community/admin/flags`).
* Attachments (`attachments.py`): images are metadata-stripped (EXIF/GPS) and
  OCR-screened; textless (scanned) PDFs are rasterized + OCR'd; text files are
  BOM-aware decoded and stored as the canonical UTF-8 of the scanned text.
  **No screening → no upload** (see `COMMUNITY_OCR_STRICT`).
* A persistent, non-dismissible notice sits under every composer:
  *"Colleague discussion only. Do not post patient-identifiable information."*

## Audit & moderation (PRD §7.5)

Every message create/edit/delete, PHI block, attachment upload, and moderation
action is recorded to the existing hash-chained audit log (`audit/audit_log.py`,
in `TEAM_DB_PATH`) — metadata only, never content. Deletes are soft
(`deleted_at`; body cleared) so the chain stays intact. Admins can delete any
message and deactivate (community-ban) a member.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `COMMUNITY_DB_PATH` | `backend/community.db` | SQLite file; point at a persistent volume in prod |
| `COMMUNITY_OCR_STRICT` | `1` (fail closed) | `0` opts into advisory-only screening when the OCR toolchain is unavailable — an explicit operator decision |
| `COMMUNITY_MAX_ATTACHMENT_BYTES` | `10485760` (10 MB) | Upload size cap |
| `COMMUNITY_BLOCK_FLAG_THRESHOLD` | `3` | PHI-block count that raises the admin repeat-offender flag |
| `COMMUNITY_DIGEST_INTERVAL_SEC` | `300` | Mention/announcement email digest flush interval |
| `COMMUNITY_SPECIALTY_MIN_MEMBERS` | `3` | Verified members of a specialty required before its channel appears (v2) |
| `COMMUNITY_EVENT_REMINDER_MIN` | `60` | Minutes before an event starts that interested members are emailed (v2.1) |
| `COMMUNITY_NEWS_ENABLED` | `0` (off) | `1` starts the scheduled #medical-ai-news content loop (v2) |
| `COMMUNITY_MORNING_ENABLED` | `0` (off) | `1` starts the morning routine: #events, #research-and-opportunities, the weekly discussion prompt, and the per-doctor 7am email. See `docs/asclepius/MORNING_ROUTINE_SETUP.md` |
| `COMMUNITY_NEWS_FEEDS` | built-in reporter set | Comma-separated `key=url` RSS overrides (v2) |
| `COMMUNITY_NEWS_KEYWORDS` | built-in AI-in-medicine list | Comma-separated keyword filter for reporter feeds (v2) |
| `COMMUNITY_DIGEST_MAX_ITEMS` | `15` | Max stories per digest post (v2) |
| `COMMUNITY_DIGEST_MAX_TOKENS` | `1200` | Token cap on the digest compose pass (v2) |
| `COMMUNITY_DIGEST_NEWS_HOUR_UTC` | `13` | Daily news-digest fire hour (v2) |
| `COMMUNITY_DIGEST_PAPERS_DOW` | `0` (Monday) | Weekly papers-digest day, Python weekday (v2) |
| `PUBMED_API_KEY` | unset | Optional free NCBI key — raises the E-utilities rate cap (v2) |

Shared prerequisites: `ASCLEPIUS_AUTH_SECRET` (stable JWT signing — required in
production), the email transport config used by `email_utils.py`, and
`PUBLIC_BASE_URL` for links in digest emails. The content digests additionally
need `ANTHROPIC_API_KEY` (curation runs on the small-model tier — pennies per
run) — without it a digest run records a failure and posts nothing.

## Host packages required for attachment screening

The Python deps (`pytesseract`, `pdf2image`, `pdfminer.six`, `PyPDF2`,
`Pillow`) are in `requirements.txt`, but two **system binaries** must be on the
host for image/scanned-PDF screening:

```
apt-get install -y tesseract-ocr poppler-utils
```

Until they are installed, image and scanned-PDF uploads are refused with a
clear "paste it as text instead" message (fail-closed default). Text files,
text-layer PDFs, and all messaging work regardless.

## Operational notes

* **Real-time**: one in-process WebSocket hub; broadcasts fan out concurrently
  with a 5s per-socket send timeout. Clients fall back to 5s polling when the
  socket drops and resync every loaded channel on reconnect. If the backend is
  ever scaled to multiple processes, the hub needs a shared bus (e.g. Redis
  pub/sub) — polling keeps the product functional in the meantime.
* **Email digests**: queued durably in `community_notifications`, flushed by a
  background loop started on app startup; one digest email per user per flush.
  Send failures are logged and not retried (at-most-once by design).
* **Retention**: messages are retained indefinitely unless an admin removes
  them; this is stated in the community footer.
* **Channels are fixed in code** (v2, +#events in v2.1): core channels —
  `#general`, `#introductions`, `#task-announcements` (admin-post, threaded
  replies open), `#events` (admin-post; hosts the pinned event cards),
  `#medical-ai-news` (bot/admin-post), `#research-and-opportunities`
  (admin-post), `#future-of-medical-ai`, `#questions-help` — plus one channel
  per ENABLED specialty, derived from `asclepius/specialties.py`. Seeding is
  idempotent on boot; a slug removed from the config is DEACTIVATED (hidden
  everywhere, history preserved), never deleted. Voice/video and user-created
  channels remain deliberately out of scope.

## Community v2 — bridge, taxonomy, bot, digests

* **Verification gate bridge.** `_passes_gate` (and `member_map`) accept a
  verified colleague from EITHER credentialing system: the Contributors-vault
  flag (`contributor_credentials.credentials_verified`) OR the PRD-B admin
  approval (`users.verification_status = 'approved'`). Before the bridge, a
  queue-approved physician could not enter the community at all.
* **Specialty channels are threshold-gated.** A specialty channel appears only
  once ≥ `COMMUNITY_SPECIALTY_MIN_MEMBERS` verified non-staff members of that
  specialty exist — and STICKS once it has messages (history is never hidden).
  Visibility is global (identical for every member), computed per request in
  `router.visible_channels`; a hidden channel 404s exactly like an unknown
  slug and is excluded from unread counts and search scope.
* **System author ("Archangel" bot).** `community/system_posts.py` posts as
  the virtual `u-system` author (never a users row, never in the directory,
  not DM-able; renders with an APP badge). The §7 PHI gate still runs on
  every system post — a finding skips the post entirely (fail-closed, no 422).
  System posts never trigger the announcement email blast; a welcome queues a
  mention digest for the welcomed member only.
* **Welcome on verification.** Queue approval (`asclepius_verify.approve_signup`)
  and the vault-flag PUT both fire a one-time `#introductions` welcome
  (`community/onboard.py`), idempotent via `users.slack_joined` (flag set
  FIRST: the safe failure is a missed welcome, never a double-post), and
  best-effort (a welcome failure never fails the decision). The approval
  email gained a community paragraph.
* **Content digests.** `community/feeds.py` (PubMed E-utilities, arXiv,
  medRxiv, reporter RSS — all free) → keyword filter → persistent dedup
  (`community_content_items`, normalized URL + 14-day title match) → two
  Claude passes (select/score then compose; role `community_digest`, small
  model tier) → one system post in `#medical-ai-news`. Runs are ledgered in
  `community_digest_runs` (three-outcome `ok`); zero fresh items or zero kept
  items = ok run, NO post; a parse failure fails the run and posts nothing;
  3 consecutive failures log an `ADMIN ATTENTION` line. The scheduler
  (daily news / weekly papers, restart-safe via the run ledger) ships OFF;
  `POST /internal/community/run-digest?kind=news|papers` (Bearer
  `INTERNAL_TOOL_SECRET`) fires a run on demand.
* **Is it actually on?** `GET /internal/community/status` (same Bearer token)
  answers in one call: whether each gate resolved on, whether the loop is
  really running, each kind's last successful run and consecutive failures,
  and whether the model key, email transport and `COMMUNITY_DB_PATH` are
  configured. Worth knowing why it exists: every way this subsystem switches
  itself off is silent. An unset gate posts nothing, a missing
  `ANTHROPIC_API_KEY` records a *successful* empty run, absent email transport
  reports 0 sent, and the default `COMMUNITY_DB_PATH` is ephemeral on Railway
  so the dedup ledger resets each deploy. From outside, all four look like a
  community with nothing to say. Startup now also logs each gate at INFO when
  on and WARNING when off.
* **Demo**: `backend/scripts/demo_community_v2.py` seeds a demo world
  (mixed bridge/vault members across specialties, a pending physician for a
  live approval, welcomes, chatter) — see its docstring for the runbook.

## Community v2.1 — events, polls, pins, bookmarks, broadcasts

A second additive pass. Store methods live in `community/store.py`; endpoints
in a NEW router module `community/social.py` (mounted beside the core router);
`.ics` + the reminder loop in `community/events.py`. `_serialize_messages`
(router) enriches every message with its `poll`/`event` payload and a `pinned`
flag, so all read paths render the cards consistently. New WS events
(`event.*`, `poll.updated`, `pins.updated`, `bookmark.*`) fan out over the hub.
New tables: `community_events`, `community_event_rsvps`, `community_polls`,
`community_poll_options`, `community_poll_votes`, `community_pins`,
`community_bookmarks`.

* **Events** — a structured object (title / start / end / IANA timezone /
  location-or-join-link / host), posted by an admin into the new `#events`
  channel and rendered as a **pinned card** at the top; past events collapse
  into a list below. Each event also gets a linked `kind='event'` message so it
  threads (the card's **Discuss** opens it). The linked message deliberately
  omits the calendar date (a full date trips the PHI exact-date rule; the card
  renders it from the structured field). **RSVP** = a one-tap Interested
  (count + list) that drives an **email reminder** `COMMUNITY_EVENT_REMINDER_MIN`
  minutes before start (in-process loop in `startup_community`, at-most-once via
  `reminded_at`, no-ops without email transport;
  `POST /internal/community/run-event-reminders` fires a sweep on demand).
  **Add-to-calendar** is integration-free: a Google Calendar template URL
  (client-side) + a downloadable `.ics` (`GET /events/{id}/calendar.ics`).
* **Polls** — any member posts a `kind='poll'` message (`POST /polls`) with 2–6
  options; single-choice voting (a re-vote replaces), author/admin can close.
  Results (counts, %, viewer's choice) ride the serialized message. Admin-post
  channels gate poll creation to admins, same as chat.
* **Pinned messages** — `POST/DELETE /messages/{id}/pin`, `GET /channels/{slug}/pins`.
  A message serializes `pinned: bool`; the 📌 action pins, the header
  "Pinned (n)" opens a side panel. A soft-delete drops the pin.
* **Channel bookmarks** — a per-channel link bar (`community_bookmarks`,
  http(s)-validated); any member adds, the adder or an admin removes.
* **@channel / @here broadcast** — `@channel` is admin-only and **durable**: a
  `*channel*` sentinel mention lights every member's badge (`unread_counts`
  OR-matches it) and queues a `broadcast` email digest for all. `@here` is
  open to any member and **ephemeral** (real-time only, no stored sentinel, no
  email). Both render as a distinct pill. The demo seed
  (`scripts/demo_community_v2.py`) now also plants an event, a poll, a pinned
  message, two bookmarks, and a broadcast.

## Direct messages

Added at the product owner's request (the original PRD deferred DMs to v2).
Physicians open a colleague's profile → **Send a message**; conversations
appear in a "Direct messages" rail section with unread pills and presence.

DMs share the channel message pipeline — the identical §7 PHI gate on every
DM write (a 1:1 case discussion is exactly where an identifier gets pasted),
the same soft delete, metadata-only audit, attachment screening, and email
digests (recipient only). Two invariants make them private:

* **Participant-only access** on every path that can reach a message by id —
  history, post, read, edit, delete, reactions, thread, attachment download,
  and search (scoped in SQL to public channels + the caller's own DMs). A
  non-participant gets the same 404 as a nonexistent message. This includes
  **admins**: there is no admin read access to others' DMs, and consequently
  no moderation inside DMs in this version — moderation applies to channels,
  plus community-wide deactivation.
* **Targeted WebSocket delivery**: DM events (`message.created/updated/
  deleted`, reactions, typing) are sent only to the two participants'
  sockets, never the broadcast.

No threads inside DMs; unread counts feed the same portal badge
(`dm_unread` + channel unread).
