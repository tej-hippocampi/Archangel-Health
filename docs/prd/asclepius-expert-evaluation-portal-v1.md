# PRD — Asclepius: Expert Evaluation Portal (Product #3 MVP)

**Working codename:** Asclepius (formerly "ExpertLoop")
**Status:** MVP / v0.1 — first revenue product (no PHI)
**Surfaced as:** its own **separate top-level tab** in the doctor portal, placed directly after the Population Analytics tab (not nested inside it)
**Code location:** all new code lives in a dedicated `asclepius` folder (`backend/asclepius/` + `frontend/asclepius/`)
**Last updated:** June 2026

---

## 0. How this PRD was adapted to our codebase (read first)

This document is the original ExpertLoop PRD **rewritten to match Archangel Health's actual architecture**. The generic stack in the source PRD (Next.js + Supabase + Postgres + Prisma + S3) does **not** match this repo. The decisions below are binding for the build; do not reintroduce the generic stack.

| Concern | Original PRD said | What we actually do here |
| --- | --- | --- |
| Frontend | Next.js + React + Tailwind | **Static vanilla HTML/JS/CSS**, served from `frontend/`. Asclepius is its own page at `frontend/asclepius/index.html`, reached via a **separate top-level nav tab** in `frontend/doctor.html`, positioned directly after the Population Analytics tab in the tab order. The React/Vite/Tailwind stack only exists in the separate marketing `landing/` app and is **not** used here. |
| Backend | Next.js API routes / Supabase | **FastAPI (Python)**. New endpoints live in `backend/routers/asclepius.py`, mounted in `backend/main.py` exactly like the existing routers. |
| Database | Supabase / Postgres / Prisma | **SQLite via raw `sqlite3`** following the `team_store.py` pattern. Asclepius gets its **own DB file** (`asclepius.db`, path via `ASCLEPIUS_DB_PATH`) owned by `backend/asclepius/store.py`. Do not add tables to `team.db`. |
| Auth | Supabase Auth / magic link | **Standalone Asclepius auth** (own user table, own JWT) inside `backend/asclepius/auth.py`. Independent of the clinical RBAC (`auth_roles.py` / tenant JWT). Reuses existing libs already in `requirements.txt` (`PyJWT`, `passlib[bcrypt]`). |
| Roles | clinician / admin / qa | New Asclepius-local roles: **`evaluator`**, **`qa_reviewer`**, **`admin`** (in the Asclepius user table, not `auth_roles.py`). |
| Tenancy | single-tenant pod | **Global internal tool** — not scoped to any health-system tenant. Tasks/submissions/exports are platform-global, gated to Asclepius-authenticated internal staff. |
| LLM | OpenAI/Anthropic behind interface | **Reuse the existing Anthropic integration**: `backend/ai/llm_client.py` `call_llm(role=...)` + `backend/ai/model_config.py` `MODEL_REGISTRY`. Add new roles `asclepius_critic` and `asclepius_candidate_gen`. Prompts live in `backend/asclepius/prompts.py` (mirroring `backend/prompts/`). |
| Export destination | S3 bucket | **Local disk** under `ASCLEPIUS_EXPORT_DIR` + a signed-ish download endpoint. S3 is **out of scope** for MVP (this repo has no S3 integration); leave a clean seam for it. |
| PHI / HIPAA | "no HIPAA apparatus" | Product carries **no PHI** (synthetic/de-identified prompts only). But because it runs inside a PHI platform, we **still** run the PHI scan on every submission and reuse the existing subprocessor BAA gate (`backend/compliance/`) for any LLM call. Anthropic is already BAA-signed (`ANTHROPIC_BAA_SIGNED=1`). |
| Tests | (unspecified) | **pytest** in `backend/tests/test_asclepius_*.py`, matching the existing 85-test suite conventions (see `backend/tests/conftest.py`). |

### Build instructions for Cursor / Claude Code
Build this as a working feature, incrementally, in the phases in §11. Prefer the patterns already in this repo over new libraries. The product's job: let a credentialed specialist evaluate AI answers fast, and emit frontier-lab-ready training data that is automatically packaged, validated, QA'd, and export-ready. There is no PHI in this product — prompts are synthetic or de-identified — so there is no per-record HIPAA apparatus; focus on speed, quality, and clean export. **All new files go under the `asclepius` folders.**

---

## 1. Summary

Asclepius is a web portal where credentialed clinicians evaluate AI-generated answers to medical prompts. For each task a specialist:

1. sees a prompt (clinical question/case),
2. sees two AI answers,
3. picks which is better (or marks both inadequate),
4. annotates / revises the better answer and critiques the worse one,
5. if both are bad, writes the ideal answer from scratch, with their reasoning and approach captured.

The moment they submit, the system packages the result into standard training formats (`{prompt, chosen, rejected}` preference pairs, ideal-answer SFT examples, and step-level reasoning traces), validates it against schema, runs automated QA + a double-check, and marks it export-ready for delivery to a frontier lab as JSONL.

**The product is the data. The app is the fastest possible way to turn a specialist's judgment into clean, verified, sellable training signal.**

---

## 2. Goals & Non-Goals

### Goals (MVP)
- A specialist can complete an evaluation task in under ~2–3 minutes, smoothly.
- Support all four output types: preference pair, revised/annotated answer, error-tagged rejection, and from-scratch ideal answer + reasoning.
- Auto-package every submission into frontier-lab-ready JSONL with provenance + annotator credentials.
- Verification pipeline: automated schema validation + quality checks + a double-check gate before anything is marked export-ready.
- Admin can load tasks, review QA, and export a delivery batch + datasheet.
- Lives cleanly in its own `asclepius` folder; reuses the existing Anthropic LLM client and the static-frontend + FastAPI + SQLite conventions.

### Non-Goals (MVP)
- No PHI handling, no patient data, no recording. (PHI scan still runs defensively.)
- No model training in-app (we produce data; buyers train).
- No payments/payout processing (track contributor hours/counts; pay manually).
- No public marketplace; internal pod of clinicians, **global internal tool** (not tenant-scoped).
- No mobile-native app (responsive web is fine).
- **No S3 / cloud-bucket delivery** in MVP (local disk export only; leave a seam).
- **No changes to the clinical RBAC** (`auth_roles.py`, tenant JWT) — Asclepius auth is standalone.

---

## 3. Users & Roles (Asclepius-local)

Roles live in the Asclepius user table (`asclepius.db`), **not** in `auth_roles.py`.

1. **`evaluator`** (clinician / specialist, primary user) — does evaluation tasks. Sees only their queue and task screen.
2. **`admin`** (operator / founder) — loads task batches, manages evaluators/credentials, reviews QA, triggers exports.
3. **`qa_reviewer`** (can be an admin or a second clinician) — performs the double-check on a sample.

Auth: email/password (passlib `pbkdf2_sha256` or bcrypt) → Asclepius JWT (HS256, `ASCLEPIUS_AUTH_SECRET`). Store credential metadata (specialty, board cert, years) on the user — this is a premium selling point and must be copyable onto every emitted record.

---

## 4. Core Doctor Flow (the spine of the product)

### 4.1 The evaluation screen (must be seamless)
Implemented in `frontend/asclepius/` (vanilla JS/CSS; reuse the visual language of `frontend/styles.css` and `doctor.html` so it feels native). Single screen, minimal friction:

1. **Prompt** displayed at top (the clinical question/case + specialty + difficulty).
2. **Two candidate answers side-by-side: Answer A and Answer B** (blinded — never show which model produced them; `generator_model` is server-side only and never sent to the eval screen).
3. **Primary verdict** — three large buttons:
   - **A is better**
   - **B is better**
   - **Both inadequate**
4. If A or B is chosen:
   - The chosen answer becomes **editable inline** — the specialist can revise it to make it correct/ideal (their revision is captured as a separate `revised_text`; the original is preserved).
   - A **"why it's better"** notes field (short free text + optional structured tags: `more_accurate`, `safer`, `better_reasoning`, `clearer`, `better_dosing`).
   - On the **rejected** answer: **error tags** (multi-select taxonomy in §6) + a one-line "why it's worse."
5. If **"Both inadequate"**:
   - A compose box opens: the specialist writes the **ideal answer from scratch**.
   - An **"approach / reasoning"** field: how they reasoned, why their answer is correct.
   - Optional step-by-step reasoning capture (add steps; see §4.2).
6. **Confidence:** quick buttons (low / medium / high).
7. **Submit** → success toast → next task auto-loads.

Keyboard-friendly and fast; default to the lightest path (pick a side, optional quick note, submit). Must survive a mid-task refresh (see §10) — draft state persisted client-side (localStorage keyed by `task_id`) and `time_spent_sec` resumed.

### 4.2 Optional step-level reasoning (for reasoning-trace tasks)
For tasks flagged `capture_reasoning: true`, the specialist adds an ordered list of reasoning steps; each step is free text and can be tagged. Produces process-reward-model data. Keep it optional and additive so it never slows the core flow.

### 4.2a Grounding Mode (evidence-anchored premium tier)
Every task/batch carries a `grounding_mode` (`optional` | `required`); see the data-optimization prompt §1.2 for full spec.
- **`optional` (default):** citing the clinical guideline/source behind a judgment is additive and never blocks Submit — protects the ≤3-min lightest path.
- **`required` (premium SKU):** the evaluator must attach at least one valid evidence anchor (to the rationale, and to each reasoning step on reasoning-trace tasks) before Submit is enabled. When this mode is active, the eval screen shows a disclaimer near the verdict: *"⏱️💲 Premium grounded task — it takes a bit more time, but grounded, guideline-cited data sells at a premium, so you earn more per task."* Premium-task counts/time are tracked separately in contributor stats for payout/credit.

### 4.3 Admin flow
1. **Load a task batch** — choose the origination mode (see §4.3a):
   - **From internal prompt bank** (default, day one): upload JSON/CSV of tasks (`prompt`, `specialty`, `difficulty`, `candidate_answers[]`) **OR** generate two candidate answers via the LLM (`call_llm(role="asclepius_candidate_gen")`).
   - **From a buyer request:** create a batch from a lab's request — grading their uploaded prompts and/or AI responses, or generating to their spec.
   - Set `grounding_mode`, `capture_reasoning`, specialty/difficulty mix, and target export profile on the batch.
2. **Assign / open queue** to evaluators (filter by specialty; default specialty = nephrology for the anchor practice).
3. **Review QA queue** (records flagged by automated checks or sampled for double-check).
4. **Export** an export-ready batch → JSONL + datasheet + quality report (tied back to the buyer request when applicable).

### 4.3a Dataset origination & buyer-request optionality
The GTM is **seed-then-expand** (see data-optimization prompt §2.5):
- **Mode A — internal seed (default):** our **anchor nephrology private practice** annotates tasks from our internal prompt bank to produce the **first sellable dataset**, with zero buyer involvement required.
- **Mode B — buyer-steered (optionality the lab unlocks):** once engaged, a lab can supply their own **prompts**, their own **AI responses** to be graded, and/or constraints (specialty, difficulty, `capture_reasoning`, `grounding_mode`, volume, export format). Modeled as a first-class **`buyer_request`** object (`draft → accepted → in_progress → delivered`); a batch can be created directly from a request, and every resulting record's provenance is stamped with `source` + request id. Buyer optionality is additive and never gates the seed flow.

---

## 5. Data Pipeline — capture → package → verify → export (CRITICAL)

This is the heart of the requirement: after the specialist submits, the data must become automatically packaged, stored, verified, double-checked, and ready to send to a frontier lab. Implemented in `backend/asclepius/` (`store.py`, `packaging.py`, `validation.py`, `critic.py`, `export.py`).

**Status lifecycle of every submission:** `submitted → auto_validated → qa_checked → export_ready → exported`

1. **Capture** — raw submission stored in `asclepius.db` with full task context + annotator identity/credentials + timestamps + `time_spent_sec`. Append a provenance entry to an `asclepius_events` audit table (mirror the `event_logs` pattern in `team_store.py`).
2. **Package** (`packaging.py`) — transform into standard training formats (see §6):
   - a preference pair `{prompt, chosen, rejected, ...}` (when A/B chosen),
   - an ideal-answer SFT example (when revised or written from scratch),
   - a reasoning trace (if steps captured).
3. **Auto-validate** (`validation.py`) — schema-valid? required fields present? non-empty? `time_spent_sec` above a configurable floor (too-fast = flag)? no accidental PHI/identifiers (regex + simple check — reuse/extend any existing de-id helper in `backend/`)? duplicate of an existing submission (hash of normalized prompt+texts)? Fail → route to QA with reason; record stays out of `export_ready`.
4. **Double-check (QA gate)** — at least one of:
   - **automated consistency check** — an LLM "critic" via `call_llm(role="asclepius_critic")` flags contradictions between the verdict, the notes, and the chosen answer; **AND**
   - **human QA** on a sampled % (configurable, default **15%**, `ASCLEPIUS_QA_SAMPLE_PCT`) and on all flagged records;
   - for a double-labeled subset, compute **inter-annotator agreement** and store it.
   - Only records that pass become `export_ready`.
5. **Export** (`export.py`) — admin generates a delivery batch: **JSONL** (one record/line) + `data_dictionary.md` + `datasheet.md` + `quality_report.md`, written to `ASCLEPIUS_EXPORT_DIR`. Served via a download endpoint. (S3/push is a future seam, not MVP.) Mark records `exported` with a provenance log entry.

**No record can reach `export_ready` without passing auto-validation AND the QA gate.**

---

## 6. Data Model — the records we emit

### 6.1 Task (input, loaded by admin)
```json
{
  "task_id": "t-neph-00231",
  "specialty": "nephrology",
  "difficulty": "hard",
  "capture_reasoning": false,
  "source": "lab_supplied",
  "prompt": "72yo on hemodialysis, K+ 6.4 with peaked T-waves. Adjust dialysate and meds?",
  "candidate_answers": [
    {"id": "A", "generator_model": "model_x", "text": "..."},
    {"id": "B", "generator_model": "model_y", "text": "..."}
  ]
}
```
> `source` ∈ {`lab_supplied`, `internal_prompt_bank`}. `generator_model` is stored server-side and **never** sent to the blinded eval screen.

### 6.2 Submission (raw, what the doctor produced)
```json
{
  "submission_id": "s-00231-7c2a",
  "task_id": "t-neph-00231",
  "verdict": "A_better",
  "chosen_id": "A",
  "rejected_id": "B",
  "chosen_revision": {
    "edited": true,
    "revised_text": "...specialist-corrected version...",
    "why_better_tags": ["safer", "better_dosing"],
    "why_better_notes": "B over-lowers dialysate K+, arrhythmia risk"
  },
  "rejected_critique": {
    "error_tags": ["dosing_error", "unsafe_recommendation"],
    "why_worse": "recommends dialysate K+ 1.0, too aggressive"
  },
  "from_scratch": null,
  "reasoning_steps": [],
  "confidence": "high",
  "annotator": {
    "id_hashed": "a91f...",
    "credentials": "board_certified_nephrology",
    "years_experience": 12
  },
  "time_spent_sec": 142,
  "status": "submitted"
}
```

When `verdict = both_inadequate`:
```json
"from_scratch": {
  "ideal_answer": "...specialist-written ideal answer...",
  "approach_notes": "confirm ECG changes first, then...",
  "reasoning_steps": [
    {"step": 1, "text": "Assess for ECG changes / cardiac instability"},
    {"step": 2, "text": "Acute lowering: calcium, insulin-dextrose; then dialysis"}
  ]
}
```

### 6.3 Packaged training records (export, frontier-lab-ready)
**Preference pair (reward models / RLHF):**
```json
{"type":"preference","prompt":"...","chosen":"<revised or original chosen text>","rejected":"<rejected text>","context":{"specialty":"nephrology","difficulty":"hard"},"rationale":"B over-lowers dialysate K+...","error_tags_on_rejected":["dosing_error"],"annotator_credential":"board_certified_nephrology","confidence":"high","agreement_score":null,"submission_id":"s-00231-7c2a"}
```
**Ideal answer (SFT):**
```json
{"type":"ideal_answer","prompt":"...","ideal_answer":"...","approach_notes":"...","annotator_credential":"board_certified_nephrology","submission_id":"s-00231-7c2a"}
```
**Reasoning trace (process reward model):**
```json
{"type":"reasoning_trace","prompt":"...","steps":[{"step":1,"text":"...","label":null}],"final_answer":"...","annotator_credential":"board_certified_nephrology","submission_id":"s-00231-7c2a"}
```

### 6.4 Error taxonomy (versioned config)
`dosing_error` · `unsafe_recommendation` · `hallucination` · `omission` · `wrong_diagnosis` · `outdated_guideline` · `misreads_labs` · `wrong_contraindication` · `other`. Each with optional severity. Store the taxonomy version on every record (mirror `APP_AI_CONFIG_VERSION` in `model_config.py`).

---

## 7. Functional Requirements

### 7.1 Auth & credentialing (standalone)
- `backend/asclepius/auth.py`: email/password login → Asclepius JWT (`ASCLEPIUS_AUTH_SECRET`, HS256). Roles: `evaluator`, `admin`, `qa_reviewer`.
- Own user table in `asclepius.db`; passwords hashed (passlib). Independent of clinical/tenant auth.
- Store evaluator credential metadata (specialty, board cert, years) — surfaced on every record.

### 7.2 Task queue
- Evaluator sees a "Next task" queue filtered to their specialty.
- Track per-task `time_spent_sec` (start when task opens, stop on submit; resume after refresh).

### 7.3 Evaluation UI (§4) — blinded A/B, inline revise, error tags, from-scratch compose, confidence, submit.

### 7.4 Packaging & verification pipeline (§5) — auto-package, auto-validate, QA gate, status lifecycle.

### 7.5 Export
- Admin selects export-ready records (filter by specialty/type/date) → JSONL + `data_dictionary.md` + `datasheet.md` + `quality_report.md` under `ASCLEPIUS_EXPORT_DIR`.
- Download endpoint returns the batch (zip or per-file). Log every export (records, timestamp, destination) to the provenance table. (S3 = future.)

### 7.6 Admin dashboard
- Queue counts by status; per-evaluator throughput + time-per-task; QA pass rate; export history.

---

## 8. Quality & "double-check" mechanisms (the buyer's trust)
- Automated validation on every record (schema, completeness, time-floor, PHI scan, duplicate).
- LLM critic consistency check (`call_llm(role="asclepius_critic")`) — verdict vs. notes vs. chosen answer agree. Audit-logged via the existing LLM audit path.
- Human QA on a sampled % + all flagged records.
- Inter-annotator agreement on a double-labeled subset (store the score; buyers want it).
- Annotator credentialing surfaced on every record — credentialed-specialist provenance is the whole premium.

---

## 9. Tech Stack (this repo's actual stack — do not substitute)

- **Frontend:** static HTML/JS/CSS in `frontend/asclepius/`, served from the existing `/static` mount (`app.mount("/static", StaticFiles(directory="../frontend"))` in `main.py`). Reached via its own **separate top-level nav tab** in `frontend/doctor.html`, placed directly after the Population Analytics tab (see §11 Phase 0 for wiring). No build step, no React, no Tailwind.
- **Backend:** FastAPI router `backend/routers/asclepius.py`, registered in `backend/main.py` alongside the other routers. Business logic in the `backend/asclepius/` package.
- **DB:** SQLite (`asclepius.db`, `ASCLEPIUS_DB_PATH`) via raw `sqlite3`, `AsclepiusStore` in `backend/asclepius/store.py` following the `team_store.py` pattern (`_conn()`, `_init_schema()` with `executescript`, `row_factory = sqlite3.Row`).
- **LLM (optional features):** Anthropic Claude via `backend/ai/llm_client.py` `call_llm(role=...)`; add `asclepius_critic` and `asclepius_candidate_gen` to `MODEL_REGISTRY` in `backend/ai/model_config.py` (default `claude-sonnet-4-6`, overridable via `MODEL_ASCLEPIUS_CRITIC` etc.). Prompts in `backend/asclepius/prompts.py`. All calls go through the subprocessor BAA gate.
- **Export:** server function → JSONL + markdown companions on disk; download endpoint. S3 = future seam.
- **Config:** all secrets/keys server-side via env vars; add Asclepius vars to `.env.example` (see §9.1).
- **Auth libs:** reuse `PyJWT`, `passlib[bcrypt]` (already in `requirements.txt`). No new auth dependency.

### 9.1 New environment variables (add to `.env.example`)
```
ASCLEPIUS_DB_PATH=             # default: backend/asclepius.db
ASCLEPIUS_AUTH_SECRET=change-me-asclepius
ASCLEPIUS_EXPORT_DIR=          # default: /tmp/asclepius-exports
ASCLEPIUS_QA_SAMPLE_PCT=15
ASCLEPIUS_TIME_FLOOR_SEC=20    # below this, flag too-fast
MODEL_ASCLEPIUS_CRITIC=        # optional model override
MODEL_ASCLEPIUS_CANDIDATE_GEN= # optional model override
```

### 9.2 Proposed folder layout
```
backend/
  asclepius/
    __init__.py
    auth.py          # standalone JWT auth + user table helpers
    store.py         # AsclepiusStore (asclepius.db, raw sqlite3)
    packaging.py     # submission -> training records
    validation.py    # schema + completeness + time-floor + PHI scan + dupe
    critic.py        # LLM consistency check (call_llm role=asclepius_critic)
    export.py        # JSONL + markdown companions + provenance log
    prompts.py       # critic + candidate-gen prompts
    schemas.py       # Pydantic models for tasks/submissions/records
  routers/
    asclepius.py     # FastAPI routes, mounted in main.py
  tests/
    test_asclepius_packaging.py
    test_asclepius_validation.py
    test_asclepius_export.py
    test_asclepius_auth.py
frontend/
  asclepius/
    index.html       # eval screen + queue
    admin.html       # admin: upload, QA queue, export, dashboard
    asclepius.js
    asclepius.css
```

---

## 10. Non-Functional
- **Speed:** task screen loads instantly; submit + auto-package < 1s perceived.
- **Reliability:** no lost submissions; safe recovery from refresh mid-task (client-side draft + resumed timer; idempotent submit keyed by `submission_id`).
- **Usability:** keyboard-friendly, blinded A/B, minimal required typing, "lightest path" default.
- **Auditability:** provenance log on every record (who, what, when, status changes, exports) in `asclepius.db`. LLM calls audited via the existing `call_llm` audit path.
- **Isolation:** zero coupling to `team.db` or clinical RBAC; Asclepius can be disabled by not mounting its router.

---

## 11. Build Phases (build in order)

**Phase 0 — Folder + wiring.** Create `backend/asclepius/` and `frontend/asclepius/`. Add `routers/asclepius.py`, register in `main.py`. Add a **separate top-level nav tab** in `frontend/doctor.html` — it is its own tab, **not** a sub-tab of Population Analytics — placed directly after the Population Analytics tab in the tab order:
- In the main nav (`<nav class="tabs">`, alongside `data-tab="roster|escalation|compliance|analytics"`), add `<button class="tab-btn" data-tab="asclepius">Expert Evaluation</button>` **immediately after** the `analytics` button.
- Add a matching `<section class="page" id="tab-asclepius">` that embeds/links `frontend/asclepius/index.html` (e.g. an iframe or an "Open portal" launch).
- It is handled by the existing `setupTabs()` switcher; add a lazy-load guard (like `complianceTabLoaded`) if the portal should load on first open.
- Add env vars to `.env.example`.

> Note: it lives in `doctor.html` next to Population Analytics for placement only; it is a fully independent tab with its own page, its own data, and its own standalone auth — nothing about it nests inside or depends on Population Analytics.

**Phase 1 — Skeleton:** standalone auth + roles; admin task upload (JSON); evaluator queue; evaluation screen with A/B + verdict + submit; store raw submission in `asclepius.db`. (No packaging yet.)

**Phase 2 — Full doctor flow:** inline revise of chosen; error tags + why-worse on rejected; "both inadequate" from-scratch compose + approach notes; confidence; time tracking + refresh recovery.

**Phase 3 — Packaging:** transform submissions → preference pair / ideal-answer / reasoning-trace records; status lifecycle.

**Phase 4 — Verification:** auto-validation checks; LLM critic (`asclepius_critic`); QA review queue; sampled human QA; inter-annotator agreement on double-labeled subset; `export_ready` gating.

**Phase 5 — Export & delivery:** JSONL export + data dictionary + datasheet + quality report to `ASCLEPIUS_EXPORT_DIR`; download endpoint; provenance log. (S3 seam left for later.)

**Phase 6 — Dashboard & hardening:** admin metrics; pytest schema-validation tests; optional reasoning-step capture; CSV task upload; optional candidate generation (`asclepius_candidate_gen`).

---

## 12. Acceptance Criteria (Definition of Done)
- The feature is reachable as its **own separate top-level tab** (placed directly after Population Analytics, not nested inside it), and all its code lives in the `asclepius` folders.
- An evaluator completes a task (all four output types supported) in ≤~3 min.
- Every submission auto-packages into schema-valid training record(s).
- No record reaches `export_ready` without passing auto-validation **and** the QA gate.
- "Both inadequate" reliably captures a from-scratch ideal answer + reasoning.
- Admin exports a JSONL batch + datasheet + quality report (to disk + download).
- Every record carries annotator credentials + provenance; export is logged.
- LLM critic + candidate generation (if used) go through `call_llm` and the BAA gate.
- App runs end-to-end in a desktop browser; pytest suite for packaging/validation/export passes.
- `team.db` and clinical RBAC are untouched.

---

## 13. Assumptions & Open Questions
- **Assumption:** prompts/candidate answers are synthetic or de-identified (no PHI). Enforce a PHI scan anyway.
- **Resolved (per build decisions):** standalone Asclepius auth; separate `asclepius.db`; global internal tool (not tenant-scoped); separate page in the `asclepius` folder surfaced as its **own top-level tab** (placed directly after Population Analytics, not nested inside it).
- **Open:** does the first buyer (a small AI medical lab) supply their own prompts + model outputs to grade, or do we generate candidates? Support both; **default to lab-supplied** for the first deal.
- **Open:** double-check policy — start with automated validation + LLM critic + 15% human QA; tune with the buyer (`ASCLEPIUS_QA_SAMPLE_PCT`).
- **Open:** final export schema — co-design the exact fields with the first buyer before scaling; their eval format defines "optimal." Keep `packaging.py` field-mapping in one place so it's easy to adjust.

*End of PRD.*
