# 02 — Technical Context

A snapshot of the codebase as of the brainstorm. Use this to ground product ideas in what's already there vs. what would be net-new engineering.

## Stack

- **Backend:** Python **FastAPI** single-service app (`backend/main.py`, ~4,000 LOC).
- **Frontend (clinician/patient app):** Static HTML + vanilla JS, served by FastAPI under `/static`. Pages: `index.html` (patient post-op dashboard), `pre-op.html`, `doctor.html`, `admin.html`, `preop-survey.html`, `voice-avatar.html`, `prompt-lab.html`, `upload.html`.
- **Frontend (marketing):** Separate **React + Vite + TypeScript** app in `landing/`. Tailwind-styled. Hosts the TEAM Calculator, TEAM whitepaper, Sign in / Sign up dialogs, tenant onboarding wizard.
- **Auth:** JWT issued by FastAPI. Tenant-scoped JWTs for health-system staff (`tenant_jwt.py`). User store is file-backed (no DB).
- **Data:** **In-memory** for the demo patient (`maria_001` re-seeded on every startup). No production database yet. PHI persistence is therefore not yet solved.
- **Deployment:** Dockerfile + `docker-compose.yml`; Railway (`railway.json`, Procfile); Cloudflare DNS for `archangelhealth.ai` / `archangelhealth.com`; landing on Vercel.
- **No tests, no linter** configured at repo level.

## Key external integrations

| Service | Purpose | Notes |
|---|---|---|
| **Anthropic API** | Digital Care Companion chat, content generation | Requires `ANTHROPIC_API_KEY`; degrades if missing |
| **ElevenLabs** | Voice synthesis for pre-op / diagnosis / post-op videos | `backend/integrations/elevenlabs.py` |
| **Tavus** | Optional video-avatar of voice scripts | `backend/integrations/tavus.py` |
| **Twilio** | SMS outreach to patients | `backend/integrations/twilio_client.py` |
| **SendGrid / SMTP** | Onboarding OTP, invite emails | Required for health-system onboarding |

## Backend layout (the parts that matter for product thinking)

```
backend/
  main.py                       # ~4k LOC; routes, app wiring, patient seeding
  auth.py                       # public auth (doctor/patient sign-in, JWT)
  tenant_jwt.py                 # health-system tenant JWT
  team_store.py                 # tenant store
  pipeline/
    ingest.py                   # EHR/PDF intake
    classify.py                 # episode / content classification
    extract.py                  # structured extraction from intake
    generate.py                 # call into prompts to produce voice scripts + battlecards
  prompts/
    diagnosis.py                # DIAGNOSIS_VOICE_PROMPT, DIAGNOSIS_BATTLECARD_PROMPT
    treatment.py                # TREATMENT_VOICE_PROMPT, TREATMENT_BATTLECARD_PROMPT
    preop.py                    # PREOP_VOICE_PROMPT, PREOP_BATTLECARD_PROMPT
    postop.py                   # POSTOP_VOICE_PROMPT, POSTOP_BATTLECARD_PROMPT
    avatar.py                   # avatar-mode prompt
    registry.py                 # PROMPT_REGISTRY for the prompt lab
  routers/
    admin.py                    # admin endpoints
    onboarding.py               # health-system tenant onboarding
    tenant_portal.py            # tenant-scoped portal endpoints
    internal.py                 # internal-only (prompt lab, etc.)
  preop_survey.py               # 10-section structured intake
  intake_form_parser.py         # parse uploaded intake forms (PDF, etc.)
  intake_section_chat.py        # conversational intake bot
  intake_section_prompts/       # one MD per intake section, with sample dialogs
```

## Key endpoints (commercial-relevant)

| Method | Path | Purpose |
|---|---|---|
| GET  | `/patient/{id}` | Patient post-op dashboard |
| GET  | `/patient/{id}/pre-op` | Patient pre-op dashboard |
| GET  | `/patient/{id}/digital-care-companion` | Voice + chat companion |
| GET  | `/api/patient/{id}/config` | Dashboard config JSON |
| GET  | `/api/patient/{id}/battlecard` | Rendered battlecard HTML |
| GET  | `/api/patient/{id}/audio` | Voice audio URL |
| POST | `/api/digital-care-companion/chat` | AI chat (Anthropic) |
| POST | `/api/process-patient` | End-to-end EHR → personalized content pipeline |
| POST | `/api/upload-pdf` | Intake PDF ingestion |
| POST | `/api/onboarding/request-otp` | Health-system onboarding (SendGrid) |
| POST | `/api/auth/login`, `/register` | Public auth |
| GET  | `/doctor/patient/{id}` | Surgeon view of one patient |
| GET  | `/admin` | Admin portal |
| GET  | `/internal/prompt-lab` | Internal prompt A/B tool |
| GET  | `/docs` | FastAPI Swagger |

## Content generation pipeline (today's "magic")

```
EHR / intake PDF / survey  ──►  pipeline.ingest
                                      │
                                      ▼
                              pipeline.extract  ──►  Clinical Input Layer (structured JSON)
                                      │
                                      ▼
                              pipeline.classify (episode / phase)
                                      │
                                      ▼
                              pipeline.generate
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
  Voice script (Anthropic)    Battlecard (Anthropic)         Avatar script
        │                             │                             │
        ▼                             ▼                             ▼
   ElevenLabs TTS                Rendered HTML                 Tavus video
```

Strict prompt rules already enforced in `prompts/`:
- No hallucinated doctor names, test results, or prognosis details ("ZERO HALLUCINATIONS").
- Hard word counts per section (e.g., 700–900 words for pre-op).
- Health literacy level 5–8.
- Tone markers (`[reassuring]`, `[grounding]`, `[firm]`, …) inserted for ElevenLabs.
- Define every medical term inline.

## What is *not* built yet (high-leverage gaps)

- **EHR integration** (HL7v2, FHIR, Epic Care Everywhere, ADT feeds). Today everything is manual upload or seeded fixtures.
- **Persistent multi-tenant database with PHI controls** (encryption at rest, audit log, BAA-ready).
- **PROM collection workflow** (HOOS-JR / KOOS-JR for LEJR — required by TEAM).
- **30/90-day readmission risk model** and surgeon-facing alerting.
- **Post-acute network steerage** (preferred SNF / home-health, leakage tracking).
- **Surgeon-facing financial dashboard** (per-episode target price, actuals, projected reconciliation).
- **Care-team orchestration** (nurse navigator task queue, escalation rules, SLA tracking).
- **Mobile app** (today everything is responsive web).
- **Formal testing / clinical-evidence pipeline** (no test suite, no QA harness for hallucinations, no clinical safety review log).
