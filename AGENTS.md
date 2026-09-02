# AGENTS.md

## Repo identity & git workflow

This repo is **`tej-hippocampi/Archangel-Health`** on GitHub (the codebase covers
CareGuide, the Asclepius evaluation portal, and the Community platform — the
product/company naming has shifted over time, but this is the one repo).
Ship work as a **pull request targeting `main`**, not a direct push to `main`.

## Cursor Cloud specific instructions

### Architecture
CareGuide is a single-service Python FastAPI app (backend) serving a static HTML/CSS/JS frontend. No database, no build step. The **landing page** (`landing/`) is a separate React (Vite) app for Elysium Health marketing/sign-in; it uses the same backend for auth (JWT). See `README.md` and `landing/README.md`.

### Running the dev server
**Backend (required for patient dashboard and landing auth):**
```
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```
**Landing (optional):** from repo root, run the backend first, then:
```
cd landing && npm install && npm run dev
```
Landing runs at `http://localhost:5173` and proxies `/api` to the backend. Sign in / Sign up use `/api/auth/login` and `/api/auth/register`. Set `AUTH_SECRET` in backend `.env` for JWT signing.

The demo patient dashboard is available at `http://localhost:8000/patient/maria_001` (seeded in-memory at startup).

### Environment variables
Copy `.env.example` to `.env`. Set `BASE_URL=http://localhost:8000` for local dev. Set `AUTH_SECRET` to a long random string for landing auth (JWT). External API keys (Anthropic, ElevenLabs, Tavus, Twilio) are optional for basic UI testing — the app gracefully degrades without them. Chat requires `ANTHROPIC_API_KEY` for real AI responses.

Health system onboarding (OTP and invite emails) requires **`SENDGRID_API_KEY`** and a **verified** `SENDGRID_FROM_EMAIL` in the same SendGrid account (or working `SMTP_*`). Without this, `/api/onboarding/request-otp` returns 503; check the backend terminal for `[email_utils] SendGrid HTTP …` diagnostics.

**TEAM eligibility (Track A)** lives in `backend/eligibility/` (parsers, extractor, evaluator, pipeline) and `backend/routers/eligibility.py`. Requires `ANTHROPIC_API_KEY` for live extraction, and `tesseract` + `poppler` (`brew install tesseract poppler`) for OCR fallback on image-only PDFs. Uploaded documents land under `$UPLOAD_DIR/eligibility/<patientId>/` (default `/tmp/elysium-eligibility`). All check / override / finalize / batch endpoints write to the in-memory audit log; view via `GET /admin/audit/eligibility`.

### Gotchas
- **Static file paths**: `frontend/index.html` uses `/static/` prefixed paths. FastAPI mounts the `frontend/` directory at `/static`. If the HTML is served at `/patient/{id}`, relative paths won't resolve — always use `/static/styles.css` and `/static/app.js`.
- **Test suite is `backend/tests/` (pytest)** — covers the eligibility evaluator, parsers, and a 50-case validation fixture set. Run with `cd backend && python3 -m pytest tests/ -q`.
- **CI runs that suite in 4 shards**, one per runner, because a single job outgrew its timeout (see `.github/workflows/tests.yml`). To reproduce one shard exactly as CI ran it: `cd backend && python3 -m pytest -q $(python3 scripts/ci_shard.py <n> 4)`. Sharding is deterministic and total — `tests/test_ci_sharding.py` asserts every test file lands in exactly one shard, so a new test file cannot silently drop out of CI. Adding a test file needs no config change; if the shards drift out of balance, refresh the weights with `python3 -m pytest tests/ -q --durations=0 | python3 scripts/ci_shard.py --measure`.
- **No `python` binary**: Use `python3` (not `python`) to run commands.
- **pip installs to user dir**: `pip install` installs to `~/.local/bin`. Ensure `$HOME/.local/bin` is on `PATH`, or use `python3 -m uvicorn` instead of `uvicorn` directly.
- **In-memory data**: All patient data resets on server restart. The demo patient `maria_001` is re-seeded on every startup.
- **CORS for the deployed landing**: the backend allowlists origins from `ALLOWED_ORIGINS` (or `BASE_URL`+`LANDING_URL`), plus a baked-in regex for `https://archangelhealth.ai` and its subdomains (`ALLOWED_ORIGIN_REGEX` to override — see `backend/http_security.py`). If sign-in from a new landing domain fails with a "Cannot reach the backend API" error, add that origin to `ALLOWED_ORIGINS` on the backend host (Railway).

### Is the data actually going to survive? (`docs/asclepius/IS_MY_DATA_SAFE.md`)

The failure that makes every other guarantee moot: no volume attached, so a
redeploy erases every account, task, submission and payout row — silently. Four
stores must be on the volume (`ASCLEPIUS_DB_PATH`, `TEAM_DB_PATH`,
`ASCLEPIUS_DATA_DIR`, and raw ingest, which follows the first). `ENV=production`
then makes the app REFUSE TO BOOT on non-durable storage, which is the actual
fix rather than a check.

`GET /api/asclepius/admin/storage/durability` answers this live and cheaply
(three syscalls, never cached), and the admin console renders a banner on every
tab when a store is not durable **or** when the gate is unarmed. Do not make
that endpoint expensive — `/storage/reconcile` is the heavy one, cached for 15
minutes, and the split exists because the question "is my data safe?" has to be
askable on every page load.

### Export & approval (the three-status split)

A submission used to carry three statuses that never talked to each other —
`earnings.status`, `submissions.status`, `records.status` — and export reads only
the third. **A record ships iff `records.status ∈ {export_ready, exported}`, and
exactly four events set it**: admin Approve, reviewer accept, the 14-day
auto-approve, and the QA tab. All four go through
`payments.apply_ledger_decision_to_records`, all four also resolve the ledger,
and `tests/test_export_approval_prd.py` enumerates them. **Do not add a fifth
path.** Full notes: `docs/asclepius/EXPORT_AND_APPROVAL.md`.

`Data → Export` is one page with five scopes, all resolved by
`_resolve_case_slice` — preview and bundle call the same function, so they cannot
disagree about what ships. The buyer CRM is retired and its tables are kept
(`docs/asclepius/BUYER_CRM_RETIRED.md`); the delivery rail is untouched.

A boot sweep (`asclepius/export_backfill.py`) makes already-paid-but-unshippable
cases exportable. It is idempotent and additive, and it takes the §0
no-data-loss snapshot around ITSELF (a by-hand before/after cannot work — the
script ships with the change). The verdict renders at the top of the Export tab,
and is also at `GET /api/asclepius/admin/export/migration-report`.

**Nothing on the migration or approval path may delete.**
`test_the_migration_cannot_delete_anything` parses the SQL out of every function
those paths reach and fails on `DELETE FROM` / `DROP TABLE` / `DROP COLUMN` /
`TRUNCATE` / `REPLACE INTO`. If you add a store call to that path, add it to
`_migration_write_path()` — a writer absent from that list is a writer nobody
proved is non-destructive.

### The onboarding demo video

The ~73 MB demo is **not in the repo** and must not be added to it. It lives in
the content-addressed asset store (`ASCLEPIUS_ASSET_STORE`), which in production
must be a **persistent volume** — on Railway, add a Volume and point that
variable at its mount path. A container filesystem is wiped on redeploy, so
without a volume the video disappears the next time you ship; the upload
endpoint refuses outright rather than accepting bytes it knows will vanish.

Upload (or replace, after a re-record) from your laptop:

```
python3 backend/scripts/upload_onboarding_demo.py \
    --base-url https://<api-host> --email <an admin account> --file demo.mp4
```

It is served by `GET /api/asclepius/assets/onboarding-demo` with HTTP Range
support, so the player's timeline actually scrubs. The `<video>` element
authenticates with a 30-minute **media ticket** (`POST …/onboarding-demo/ticket`)
rather than the session token, because a `<video src>` cannot send an
Authorization header and a session token in a query string ends up in access
logs. MP4 (H.264 + AAC) plays everywhere; .mov is accepted but warns.

### Claude Code healthcare plugins
`.claude/settings.json` enables two Agent Skills from Anthropic's [`anthropics/healthcare`](https://github.com/anthropics/healthcare) marketplace for everyone working on this repo in Claude Code (you'll be prompted to trust/install them on first launch):
- **`fhir-developer@healthcare`** — FHIR R4 reference (resource structures, LOINC/SNOMED/RxNorm coding, SMART-on-FHIR auth) for EHR interop work.
- **`prior-auth-review@healthcare`** — Anthropic's demo payer-review skill; use its waypoint/rubric architecture as the reference pattern for TEAM eligibility review and clinical-necessity workflows.

These are **dev-time references only** — they are not wired into the product runtime and must not be put in any PHI path (the server-side Skills API is not HIPAA-eligible). The marketplace also offers ICD-10, CMS Coverage, NPI Registry, and PubMed MCP connectors that can be enabled by adding entries to `enabledPlugins`.

### Key endpoints
| Endpoint | Method | Description |
|---|---|---|
| `/patient/{id}` | GET | Patient dashboard (HTML) |
| `/api/patient/{id}/config` | GET | Dashboard config JSON |
| `/api/patient/{id}/battlecard` | GET | Battlecard HTML |
| `/api/patient/{id}/audio` | GET | Voice audio URL |
| `/api/digital-care-companion/chat` | POST | AI chat (requires `ANTHROPIC_API_KEY`) |
| `/api/process-patient` | POST | Full EHR pipeline |
| `/api/auth/register` | POST | Landing: create account (email, password, optional name) |
| `/api/auth/login` | POST | Landing: sign in (email, password) |
| `/api/auth/me` | GET | Landing: current user (Bearer token) |
| `/api/eligibility-draft-patient` | POST | Allocate a draft patient before file upload (TEAM) |
| `/api/eligibility-documents` | POST/DELETE | Upload / remove eligibility documents |
| `/api/eligibility-checks` | POST | Start a parse → extract → evaluate pipeline |
| `/api/eligibility-checks/{id}/stream` | GET | SSE progress (status / result / error) |
| `/api/eligibility-checks/{id}/override` | POST | Audited verdict override |
| `/api/eligibility-checks/{id}/finalize` | POST | `SAVE_AS_TEAM` / `SAVE_AS_STANDARD` |
| `/api/eligibility-batches` | POST | Group upload with identity fan-out |
| `/api/eligibility-batches/{id}/stream` | GET | SSE for batch progress |
| `/admin/audit/eligibility` | GET | TEAM audit log viewer |
| `/api/asclepius/assets/onboarding-demo` | GET | Onboarding demo video (Range/206, auth or media ticket) |
| `/api/asclepius/admin/assets/onboarding-demo` | POST | Admin: upload/replace the demo video |
| `/api/asclepius/admin/earnings/{id}/approve` | POST | Admin: approve one case — ledger **and** export gate together |
| `/api/asclepius/admin/export/case-preview` | GET | Export slice preview, incl. what is EXCLUDED and why |
| `/api/asclepius/admin/export/approve` | POST | Approve every unapproved submission the preview listed |
| `/api/asclepius/admin/export/case-bundle` | POST | Build the bundle (optionally + send to a buyer) |
| `/docs` | GET | Swagger UI |
