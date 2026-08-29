# AGENTS.md

A map of this repo for coding agents. Read the landmines before deleting anything.

## What this company sells

Archangel Health sells **clinician-verified medical AI data** to frontier labs:
evaluation sets, preference and process-supervision records, and RL environments,
each annotated by a credentialed physician and shipped with provenance. The
product surface is the **Asclepius** portal, where physicians do that work.

A peri-op patient product (surgical risk tiering, discharge education, intake
interviews) was built first and is **being retired**. Most of what looks like
"the app" is that. See *The retirement* below before you touch it.

## Repo identity & git workflow

`tej-hippocampi/Archangel-Health`. Ship work as a **pull request targeting
`main`**, never a direct push. Backend is a single FastAPI service
(`backend/main.py`) serving static HTML/JS from `frontend/`; `landing/` is a
separate React/Vite app that uses the same backend for auth.

```
cd backend && python3 -m uvicorn main:app --reload --port 8000   # backend
cd landing && npm install && npm run dev                          # landing :5173
cd backend && python3 -m pytest tests/ -q                          # ~4,100 tests, ~13 min
cd backend && python3 scripts/ci_shard.py 1 4                      # what CI shard 1 runs
```

## The three planes

| Plane | Where | What it is |
|---|---|---|
| **Product** | `backend/asclepius/` (~52k lines), `routers/asclepius*.py`, `frontend/asclepius/` | The eval portal. Own SQLite DB, own auth, never touches the clinical RBAC. Case generation, rubrics, paired review, packaging, export, payments, credentialing. |
| **Doctors** | `backend/community/`, `frontend/` community pages | The physician community that supplies annotators — feed, digests, events, newsletter. |
| **Next products** | `backend/gold/`, `backend/asclepius/environments/` | Gold Standard conversation capture, and V5 clinical RL environments. |

Supporting and live: `ai/` (LLM client), `audit/` (ePHI access log), `compliance/`,
`integrations/stt/`, `field_crypto.py`, `auth.py`, `http_security.py`,
`ratelimit.py`, `email_utils.py`, `onboarding_emails.py`, `token_revocation.py`.

## Landmines — verified by tracing, not guessed

1. **`/api/auth/*` in `main.py` is LIVE.** `landing/src/lib/auth-api.ts` calls
   `/api/auth/login` and `/api/auth/register`. Those handlers consult
   **`team_store.py` (4,783 lines)** for the tenant-SSO redirect and email
   verification. It looks legacy; it is load-bearing for every signup.
   **`team_store.py` and `tenant_utils.py` stay** — candidates for a 3-function
   shim, not deletion.
2. **`routers/tenant_portal.py` is LIVE.** It serves
   `POST /api/tenant/{slug}/auth/login`, which the landing app calls from three
   places, and `main.py`'s login *redirects users to the page that calls it*.
   Deleting it breaks every health-system sign-in.
3. **`scripts/ci_shard.py` is wired into CI** (`.github/workflows/tests.yml`) and
   `tests/test_ci_sharding.py` asserts its TOTAL property. Deleting it kills CI.
   When you delete a test file, remove its `WEIGHTS` entry — fix the map, never
   the test.
4. **`frontend/doctor-sign-in.html` redirects to `/doctor/app`** on all four of
   its success paths, and that route opens `frontend/doctor.html` with **no
   existence guard**. Deleting `doctor.html` 500s the live asclepius handoff.

## Protected scripts — all five are wired in

| Script | Why it lives |
|---|---|
| `ci_shard.py` | CI depends on it (landmine 3). |
| `email_preview.py` | Renders the LIVE `onboarding_emails`. |
| `purge_community.py` | Enforces "production community starts EMPTY"; twin of `community/store.py::purge_generated_content`. |
| `smoke_multimodal.py` | The only live-LLM test of case generation. |
| `asclepius_contributor_admin.py` | Merges duplicate SSO accounts on the live DB. |

## The retirement

The peri-op product is being removed in phases. Its remaining surface —
intake interviews, process-discharge/pre-op, escalations, pre-op surveys, the
patient dashboard, avatar chat, plus the triage-config and prompt endpoints in
`routers/admin.py` and all of `routers/internal.py` — is registered **only while
`ARCHANGEL_LEGACY_PERIOP=1`** (the default). See `backend/legacy_flag.py`.

- Flag **on**: the route table is byte-identical, in the same order. Shipping is
  a no-op.
- Flag **off**: 72 routes are withheld and requests 404 exactly as they will once
  the code is deleted.

**Nothing further gets deleted until the flag has been off in production for a
week with clean access logs** — the watch list is `docs/dark-week-watch-list.txt`
(the 72 gated routes, plus `/doctor/app`, which is not gated but is the only way
into the legacy dashboard, so hits there decide whether it can be deleted). A 404 on a gated path is a live consumer that
tracing missed — tracing finds every caller that is written down, not the cron
job on someone's laptop or the partner integration. Still awaiting deletion
behind that week: `triage/`, `pipeline/`, `eligibility/`, most of `prompts/`, the
gated route bodies in `main.py`, `frontend/{doctor,index}.html`, `app.js`,
`postop.js`.

**`prompts/` does not come out in one piece.** `prompts/registry.py` is a hub:
`ai/llm_client.py:238` imports it for LLM audit telemetry, and it in turn imports
`pipeline.*` and `triage.intraop.extractor_llm` at module level — so deleting
those two packages breaks `ai/`, which is live. `gold/` needs `prompts/gold.py`.
Trim `registry.py` down to `.gold` + `asclepius.prompts` FIRST, then delete
`prompts/{diagnosis,treatment,preop,postop,avatar,system}.py`; keep `gold.py` and
the trimmed registry. `prompts/eligibility.py` goes with `eligibility/`.

### The Phase 5 index — every last consumer, in one grep

```bash
grep -rn "# PHASE-5:" backend/          # the marked sites
```

| What | Last consumer | Notes |
|---|---|---|
| `integrations/tavus.py` | `main.py:59` only | delete with its `TAVUS_*` vars |
| `integrations/twilio_client.py` | `main.py:60` only | delete with `TWILIO_*`, `CARE_TEAM_PHONE` |
| `integrations/elevenlabs.py` | `main.py:58`, `pipeline/gated_synthesis.py:5`, `routers/internal.py:21`, and two tests | **not** a single consumer — the two non-test extras are themselves Phase 5 deletions, so they resolve, but check in that order |
| `frontend/telehealth-{join,room,setup}.html` | nothing | already orphaned: their routes died with `routers/telehealth.py` |
| `.claude/skills/surgical-risk-triage/` | — | documents `triage/`, a Phase 5 deletion. Retire the skill in the same PR. |
| `.claude/skills/team-eligibility-review/` | — | documents `eligibility/`, a Phase 5 deletion. Same. |

Both skills were edited by the cleanup to drop references to already-deleted
routers, which keeps `tests/test_skills_sync.py` green — but they still describe
workflows whose code is gated for deletion. **Do not follow either skill into
peri-op code expecting it to survive.**

Env vars: `DAILY_API_KEY`, `VIDEO_PROVIDER` and `DAILY_DOMAIN` have **no reader**
and are deletable now. `TAVUS_*`, `ELEVENLABS_*`, `TWILIO_*` and `CARE_TEAM_PHONE`
are each read by exactly one gated module — delete them in the same change that
deletes the code, not before. Never delete `TEAM_DB_PATH`: `team_store` backs the
live `/api/auth/*` path and survives Phase 5 by design.

**Everything already deleted is recoverable in full from the
`claude/legacy-periop-archive` branch.** Notably the X12 270/271 eligibility
parser (`backend/eligibility/parse_x12.py`) — start there for future payer work
rather than re-deriving it.

Clinical knowledge worth keeping was salvaged forward into
`backend/asclepius/clinical_flags.py`: lab / medication / ICD-10 flag derivation,
plain dicts in, flag set out. Not wired into the case pipeline.

## Before you delete anything

```bash
cd backend
python3 -c "from main import app"                     # boots
python3 -m pytest tests/ -q                            # full suite — not a subset
python3 scripts/ci_shard.py 1 4                        # sharding still total
```

Then check for **dangling imports of what you removed** — a module that imports
a deleted one from inside a *function body* fails only when that line runs, and
neither an import-time check nor a route diff will see it:

```bash
python3 scripts/check_dangling_imports.py triage pipeline    # exits non-zero on hits
```

That script exists because exactly this escaped once: `eligibility/pipeline.py`
imported `UPLOAD_DIR` from a router being deleted, at two function-local call
sites, and only the full suite caught it.

**A subset of the suite cannot see reverse dependencies or repo-wide invariant
tests. Run the whole thing.**
