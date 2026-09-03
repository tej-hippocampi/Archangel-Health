# The Sandbox realm

A full copy of the product, on production, that real users can never see.

Every request carries a **realm** — `live` or `sandbox`. The sandbox realm routes
to physically separate databases, asset directories and an email outbox, so ten
fake doctors, a fake admin and fake-onboarded organizations exercise the *exact*
production code and environment while being unable, by construction, to appear
anywhere a real user or the real admin looks.

Implements `PRD_SANDBOX_REALM`. Section numbers below cite it.

## Why a boundary, not a filter (§0)

The legacy `mockadmin` account works by *filtering* mock rows out of ~30 call
sites. That is an allow-list: every new query that forgets the filter leaks. The
sandbox uses the opposite mechanism — **sandbox rows live in different files**.
The live admin opens `asclepius.db`; sandbox data is in `asclepius_sandbox.db`;
no query can cross because there is no shared table. `mockadmin` is untouched.

## Plumbing (§1)

| Piece | Where |
|---|---|
| Realm ContextVar, derived paths, `RealmProxy`, `read_live()` | `backend/realm.py` |
| Per-realm store accessors | `asclepius.store.get_store`, `community.store.get_community_store`, `team_store.get_team_store`, `asclepius.assets._store_root`, `asclepius.export.export_root`, `asclepius.ingestion.quarantine_root` |
| Realm middleware (token claim → path → header) | `realm.RealmMiddleware`, added in `main.py` |
| Token `realm` claim | Asclepius session + media ticket, HS portal cookie, landing JWT, tenant staff JWT |
| The seams | `email_utils` (outbox), `payments.mark_paid` (403), buyer deliveries (403), export/DLA stamps |
| Realm-iterating loops | verification agent, assignment sweep, task-notify drain, community digest/news/events/morning |

**How a request gets its realm.** A token's `realm` claim always wins; a
mismatch with the header or a `/sandbox/*` path is `401 realm_mismatch`. The
`X-Asclepius-Realm` header is consulted only on unauthenticated entry points
(login, signup, the onboarding wizard). `/sandbox/*` paths are the sandbox.

**Sign-in semantics.** Sandbox accounts exist only in the sandbox DB.
`archangelhealth.ai/?realm=sandbox` → Sign in → `sb-labeler-1@…` works; the same
credentials without the param are a plain 401, because the live DB has no such
user. A sandbox account cannot land in live, and a real doctor cannot stumble
into the sandbox.

**Paths are derived, never configured** (§1.1, §7). `<name>_sandbox.db` beside
each live DB; `<root>/sandbox/` for the asset, export and raw-ingest
directories. There is no `*_SANDBOX_DB_PATH` — a test asserts it.

## Entry points

| Live | Sandbox |
|---|---|
| `/asclepius` | `/sandbox/asclepius` |
| admin console (inside `/asclepius`) | `/sandbox/admin` |
| `/provider` | `/sandbox/provider` |
| `/workspace` (buyer) | `/sandbox/buyer`, `/sandbox/workspace` |
| `/community` | `/sandbox/community` |
| landing `/` | landing `/?realm=sandbox` |

The sandbox shells are the identical HTML with one injected tag that sets
`window.__REALM='sandbox'`, wraps `fetch` with the realm header, and paints the
**realm banner** — top of viewport, lime, not dismissible, naming the sandbox
admin — before any page module runs. The page modules key their stored tokens
as `asclepius_token_sandbox`, so a live and a sandbox session coexist in one
browser.

## Seeding (§2)

`POST /api/asclepius/sandbox/seed` (sandbox admin) or, from a shell:

```
cd backend
ASCLEPIUS_SANDBOX_ADMIN_PASSWORD=… ASCLEPIUS_SANDBOX_DOCTOR_PASSWORD=… \
  python3 scripts/sandbox_seed.py [--fresh] [--reset]
```

Creates `sandbox-admin@archangelhealth.ai`, the ten physicians
(`sb-labeler-1…7`, `sb-reviewer-1…3`, specialty-spread, approved, real-data
approved, first run complete, practice case passed, referral codes issued,
community-welcomed), and the standard community channels. Idempotent by email.
`--fresh` adds one un-onboarded physician for walkthrough testing. Credentials
render on the sandbox admin's **Accounts** tab with copy buttons — never in the
repo, never in logs.

## The sandbox admin console (§3)

Same console, sandbox realm, plus a **Sandbox** section:

- **Accounts** — the ten doctors + credentials, the fake-onboarding doors, `Seed
  sandbox`, `Seed fresh doctor`, `Reset sandbox` (typed confirmation; drops and
  reseeds the three sandbox DBs and the sandbox asset dir, refusing any path
  that is not a derived sandbox path), and the snapshot-copy panel.
- **Outbox** — every email the sandbox "sent": to, subject, the OTP code / magic
  link / DLA link extracted and clickable, the rendered HTML.
- **Data → Systems** gains an `origin` chip: `production copy · <date>` or
  `sandbox onboarded`.

## Real data in the sandbox (§4)

`POST /api/asclepius/sandbox/copy-health-system/{hs_id}` copies one live health
system — row, uploads (+ raw blobs and referenced image assets into the sandbox
directories), ingest cases, upload links, and the portal accounts that carry
purpose resolutions (passwords made unusable) — stamped `origin='production'`,
`copied_at`, `source_hs_id`. Not copied: tasks, submissions, physicians. Re-copy
replaces. The live store is opened through `realm.read_live()` with `?mode=ro`;
`asclepius/sandbox_copy.py` is the only caller (AST-asserted). The committed
patient bundles are copyable through the same button as `Archangel (fixture)`.

## Fake onboarding (§5)

No new flows. The physician wizard and the HS signup/intake/DLA flows run
unchanged with `?realm=sandbox`. OTPs, welcome emails and DLA sign requests land
in the Outbox. The verification agent runs a pass per realm. The DLA PDF header
is stamped `SANDBOX — test signature, not a real agreement`.

## Invariants and their tests (§6)

| # | Invariant | Test |
|---|---|---|
| 1 | No shared table — disjoint paths for every store | `test_sandbox_realm_plumbing.py` |
| 2 | Token realm wins, both directions | `test_sandbox_realm_middleware.py` |
| 3 | **The leak test** — every live GET from the route table, zero sandbox ids | `test_sandbox_leak.py` |
| 4 | No disbursement / no delivery; ledger still moves | `test_sandbox_seams.py` |
| 5 | Live never written by sandbox (`?mode=ro`, one caller) | `test_sandbox_realm_plumbing.py`, `test_sandbox_copy.py` |
| 6 | Reset is sandbox-only, refused before any file is touched | `test_sandbox_seed.py` |
| 7 | Realm banner renders on every `/sandbox/*` route (Playwright) | `test_sandbox_admin_ui.py` |
| 8 | No import-time store pins | `test_sandbox_realm_plumbing.py` |
| 9 | Config parity — sandbox paths derived, never read | `test_sandbox_realm_plumbing.py` |

Leak test output at the time of writing (`pytest tests/test_sandbox_leak.py -s`):

```
═══ Sandbox leak test (PRD §6.3) ═══
sandbox-only identifiers: 120
sandbox activity: 10 tasks, 10 submissions, 1 org signup (ops@sandbox-general.example), export=built, 1 community post
live admin GET endpoints exercised (from app.routes): 147
server errors: 0
direct by-id lookups of sandbox rows from live: 3 — all refused
leaks: 0 — ZERO sandbox rows visible to the live admin
```

## Ops — Railway variables, exactly (§7)

Add two secrets:

| Variable | Why |
|---|---|
| `ASCLEPIUS_SANDBOX_ADMIN_PASSWORD` | Enables the realm. Unset → every `/sandbox/*` route 404s and the realm header is ignored, so the feature is dark until you turn it on and can be turned off instantly by deleting the var. Also the sandbox admin's password. |
| `ASCLEPIUS_SANDBOX_DOCTOR_PASSWORD` | The shared password for the ten seeded physicians. Separate so it can be rotated or handed to a tester without exposing admin. |

**Do not add** any `*_SANDBOX_DB_PATH` / `*_SANDBOX_ASSET_STORE`. Sandbox paths
derive from the live ones (`/data/asclepius.db` → `/data/asclepius_sandbox.db`,
`ASCLEPIUS_ASSET_STORE/sandbox/`), so they land on the same persistent volume.

**Edit nothing else.** `ENV`, `ASCLEPIUS_DB_PATH`, `COMMUNITY_DB_PATH`,
`TEAM_DB_PATH`, `ASCLEPIUS_ASSET_STORE`, `DEMO_MODE` and the mock-account vars
are read unchanged. `mockadmin` keeps working in the live realm exactly as today.

**Backups:** when a backup script is written it must skip `*_sandbox.db` and the
`/sandbox/` directories; until then the sandbox DBs are disposable — `Reset
sandbox` is the recovery path.

**Turning it on:** set the two variables, redeploy (the sandbox admin is ensured
at boot), open `/sandbox/admin`, sign in as `sandbox-admin@archangelhealth.ai`,
press **Seed sandbox** on the Accounts tab.
