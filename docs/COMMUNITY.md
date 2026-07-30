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
| Portal entry | "Community" item in the portal header (`asclepius.js`), opens a new tab; unread badge polls `/api/community/badge` |
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

Shared prerequisites: `ASCLEPIUS_AUTH_SECRET` (stable JWT signing — required in
production), the email transport config used by `email_utils.py`, and
`PUBLIC_BASE_URL` for links in digest emails.

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
* **Channels are fixed** (v1): `#general`, `#task-announcements` (admin-post,
  threaded replies open to all), `#questions-help`. Seeding is idempotent on
  boot. DMs, voice/video, and user-created channels are deliberately out of
  scope for v1.
