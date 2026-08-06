# Repo context - start here

| | |
|---|---|
| **What this is** | A concise, code-free map of the whole repo, for a person or an AI agent who needs to understand the structure without reading the backend and frontend end to end. |
| **Who reads it** | Anyone (human or Claude) picking up work here, especially when writing PRDs or planning a change and needing to know where things live. |
| **What it is NOT** | Not a spec, not API docs, not a source of truth for behavior. When this disagrees with the code, the code wins. |
| **Kept current by** | The `syncing-repo-context` skill (`.claude/skills/syncing-repo-context/`), run on demand. See "Keeping this current" below. |

Read this file first, then `architecture.md` for how the pieces fit, then `repo-state.md` for what changed recently and what is in flight.

---

## What this repo is

One repository, `tej-hippocampi/Archangel-Health`. The product naming has shifted over time; the same codebase covers three surfaces:

- **CareGuide** - the surgical patient platform (EHR intake, risk triage, patient dashboard, telehealth, Medicare TEAM eligibility).
- **Asclepius** - the LLM clinical-evaluation portal (benchmark cases, calibration, buyer/provider/advisor views).
- **Community** - the physician community platform (feeds, events, digests, notifications).

All three run out of one Python FastAPI backend plus a static frontend, with a separate React landing/marketing site.

## Top-level layout

| Path | What lives here |
|---|---|
| `backend/` | The product runtime. Python / FastAPI. Serves the JSON API AND the static frontend. No database (in-memory), no build step. |
| `frontend/` | Static HTML / CSS / JS UIs (patient, doctor, asclepius, buyer, provider). Served by the backend at `/static`. No build step. |
| `landing/` | Separate React (Vite) marketing + sign-in site. Deploys to Vercel. Shares the backend for JWT auth. |
| `docs/` | Runbooks, deployment guides, and the PRD suites (`docs/prd/`, `docs/security/`, `docs/asclepius/`). This context folder lives at `docs/context/`. |
| `design/`, `canvases/`, `sample_ehrs/` | Design assets, canvas mockups, and sample EHR inputs used in demos and tests. |
| `.claude/` | Claude Code skills (`skills/`) and `settings.json` (healthcare plugins). |
| `Dockerfile`, `docker-compose.yml`, `railway.json`, `backend/Procfile` | Deploy config (backend runs on Railway). |
| `demo.html`, `index.html`, `prompt-lab-spec.md` | Root-level demo pages and a prompt-lab spec. |

## Component map

```
                        ┌─────────────────────────┐
   browser ───────────▶ │  backend/  (FastAPI)     │
   (patient / doctor)   │                          │
                        │  main.py  ── mounts ────▶ routers/      (HTTP endpoints)
   frontend/ (static) ◀─┤  serves /static           pipeline/     (EHR → script)
                        │                            triage/       (risk tiering)
                        │                            eligibility/  (Medicare TEAM)
                        │                            asclepius/    (LLM eval portal)
                        │                            community/    (feeds/events)
                        │                            integrations/ (ElevenLabs/Tavus/Twilio)
                        └──────────▲───────────────┘
                                   │ /api/auth/* (JWT)
   landing/ (React, Vercel) ───────┘
```

## Module / file roots (where to start reading)

- **App entry:** `backend/main.py` (large; mounts every router, serves the frontend, seeds demo data at startup).
- **Endpoints:** `backend/routers/` (eligibility, triage tiers, `asclepius_*`, community, telehealth, leads, gold, onboarding, tenant portal, admin).
- **EHR pipeline:** `backend/pipeline/` (`ingest.py` → `extract.py` → `classify.py` → `generate.py` → `grounding_gate.py`).
- **Risk triage:** `backend/triage/` (`initial_tier.py`, `intraop/`, `postop/`, `preop_retier/`, `*_flags.py`, `tuning.py`).
- **TEAM eligibility:** `backend/eligibility/` (`parse_x12.py`, `parse_pdf.py`, `parse_csv.py`, `extract.py`, `evaluate.py`, `pipeline.py`).
- **Asclepius eval portal:** `backend/asclepius/` (`cases.py`, `calibration.py`, `credentialing.py`, `environments/`, `buyer_profiles/`, `compensation.py`).
- **Community:** `backend/community/` (`feeds.py`, `events.py`, `digest.py`, `notify.py`, `phi_gate.py`, `ws.py`).
- **Auth + security:** `backend/auth.py`, `auth_roles.py`, `tenant_jwt.py`, `http_security.py`.
- **Prompts:** `backend/prompts/` (per-domain LLM prompt registry).
- **Tests:** `backend/tests/` (pytest; includes the 50-case eligibility validation set).

## Related docs (already in the repo)

- `docs/prd/README.md` - the triage PRD suite integration map.
- `docs/security/README.md` - the HIPAA / compliance control package.
- `docs/asclepius/PRODUCT_STATE.md` - product state for the eval portal.
- `AGENTS.md` - the agent operating guide (git workflow, dev server, env vars, gotchas, key endpoints).
- `README.md` (root) - how to run locally + TEAM eligibility reference.

## The stack

- **Backend:** Python 3, FastAPI + Uvicorn. In-memory data (resets on restart). Deployed on Railway (`railway.json`, health check `/docs`).
- **Frontend:** vanilla HTML / CSS / JS, served static by the backend. No bundler.
- **Landing:** React + Vite + MUI + Radix. Deployed on Vercel (`landing/vercel.json`). Talks to the backend over `/api`.
- **External services (optional, degrade gracefully):** Anthropic (chat + extraction), ElevenLabs (voice), Tavus (avatar video), Twilio (SMS), SendGrid (email).

## Keeping this current

This folder is designed to stay fresh **as things change**, updated by your Claude coworker rather than a CI job:

1. Anyone working in the repo runs the `syncing-repo-context` skill ("sync repo context").
2. The skill reads the last-synced commit from `ingestion-log.md`, sweeps `git log <that commit>..HEAD`, and updates `repo-state.md` (and this file's layout, if new top-level modules appeared).
3. It appends what it learned to `ingestion-log.md` and advances the watermark to `HEAD`.

`repo-state.md` and `ingestion-log.md` are written **only** by that skill. This README and `architecture.md` are curated (the skill proposes changes to them but does not silently rewrite architecture).

---

*Last updated: 2026-08-06. If the top-level layout changes (a new top-level directory, a new backend domain), update the layout table and the module roots here. `repo-state.md` tracks the finer-grained "what changed since last sync".*
