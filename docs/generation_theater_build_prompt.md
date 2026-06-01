# Generation Theater — Live Pipeline + Safety-Check Visualization (Cursor Build Prompt)

> Paste into Cursor. This turns the doctor's "generating materials…" spinner into a
> **live, choreographed view of the pipeline**: the doctor watches the chart get
> read, the materials get written (with the exact **model + prompts** shown), and —
> the centerpiece — the **grounding safety check** build its checklist, tick each
> clinical item as it's verified, count up coverage/faithfulness scores, and stamp a
> PASS / REVIEW / BLOCK verdict. It makes the safety work *visible* and the product
> feel alive. Design quality is a hard requirement — see §4.

**Depends on:** prompts #1 (`ai/model_config.py`, `prompts/registry.prompt_meta`),
#2 (`pipeline/grounding_check.py`), and #4 (`pipeline/grounding_gate.py`,
`pipeline/gated_synthesis.py`) being applied. If grounding isn't present, the
Safety-review stage degrades gracefully (skipped) — guard for it.

---

## 0. What exists today (verified)

- **Doctor UI:** `frontend/doctor.html` — static vanilla HTML/JS. JWT in
  `localStorage.authToken`; all calls go through `apiJson(path, opts)` (adds the
  Bearer header). The "add patient" modal currently shows
  `<div class="processing-overlay" id="processing">` with a spinner and a progress
  bar driven **manually** from 30% → 100% (`progressBarFill.style.width`), then a
  toast. Generation is triggered (~lines 4064–4110) by:
  - post-op → `POST /api/process-discharge`
  - pre-op  → `POST /api/process-preop`
  Design tokens are CSS variables in `:root` (lines 9–24): `--bg #f6f7fb`,
  `--surface #fff`, `--line #e5e7eb`, `--text #111827`, `--muted #6b7280`,
  `--accent #2563eb`, `--accent-dark #1d4ed8`, `--ok #16a34a`, `--warn #d97706`,
  `--danger #dc2626`, `--radius 12px`, `--font Inter`. **Reuse these.**
- **Backend endpoints** in `backend/main.py`: `process_discharge` (two-resource
  post-op: extract → `generate_two_resources` → gate → synth), `process_preop`
  (single pre-op: extract → `generate(...,"pre_op")` → gate → synth). All are plain
  `async def` that await every stage and return one JSON at the end. Auth =
  `Depends(get_current_user_optional)`.
- **No streaming anywhere** for generation. You're adding it.
- **Stage data you can surface** (after deps): extraction model via
  `model_config.resolve("extraction")["model"]`; generation model + prompt
  fingerprints via `resolve("generation")` and `prompt_meta(prompt_id)` (→
  `{prompt_id, version, sha}`); the deterministic grounding checklist via
  `build_required_items(structured_data, track)`; the verdict + per-item results +
  `compute_accuracy(report)` via `check_grounding` / `audit_and_gate_script`; judge
  model + `GROUNDING_PROMPT_V`.

---

## 1. Backend — stream the pipeline as events

### 1a. One event-emitting pipeline generator

Create `backend/pipeline/streaming.py`. Refactor the shared post-op and pre-op
flows into async generators that **yield event dicts at each milestone** and do the
real work, ending with a terminal `complete` event whose payload equals what the
current endpoint returns (so nothing downstream changes).

```python
from typing import AsyncIterator
import time

def _ev(stage, status="ok", **data):       # one event
    return {"stage": stage, "status": status, "ts": round(time.time(), 3), **data}

async def run_postop_stream(input_data, *, patient_id, ctx) -> AsyncIterator[dict]:
    yield _ev("pipeline.start", track="post_op", patient_name=input_data.patient_name,
              tracks=["post_op_diagnosis", "post_op_treatment"])

    # 1) EXTRACT
    yield _ev("extract.start", model=resolve("extraction")["model"], prompt=prompt_meta("ehr_extract"))
    structured = await ExtractionLayer().extract(raw_package)
    yield _ev("extract.done", summary=_chart_summary(structured),
              missing_critical_data=structured.get("missing_critical_data", []))

    # 2) GENERATE (emit model + prompt fingerprints BEFORE the call — they're known)
    for track, vid, bid in [("post_op_diagnosis","diagnosis_voice","diagnosis_battlecard"),
                            ("post_op_treatment","treatment_voice","treatment_battlecard")]:
        yield _ev("generate.start", track=track, model=resolve("generation")["model"],
                  prompts=[_p(vid), _p(bid)])      # _p -> {prompt_id,label,version,sha}
    resources = await GenerationLayer().generate_two_resources(structured)
    for track in ("post_op_diagnosis","post_op_treatment"):
        yield _ev("generate.done", track=track, word_count=_words(resources[...]))

    # 3) GROUNDING — emit the checklist FIRST (this is "what it's checking for"),
    #    then the verdict. Use the gate so it also persists + auto-regens on BLOCK.
    for track, key, regen in (...):
        required = build_required_items(structured, track)
        yield _ev("grounding.start", track=track, judge_model=resolve("grounding_judge")["model"],
                  prompt_version=GROUNDING_PROMPT_V, required_items=required)
        yield _ev("grounding.checking", track=track, phase="coverage")
        gate, audio = None, None
        gate = await audit_and_gate_script(patient_id=patient_id, structured_data=structured,
                                           script=resources[key]["voice_script"], track=track,
                                           team_store=ctx.team_store, regenerate_fn=regen)
        if gate.regenerated:
            yield _ev("grounding.regenerated", track=track, reason="first draft blocked; redrafted")
        acc = compute_accuracy(gate.report)
        yield _ev("grounding.result", track=track, verdict=gate.report.verdict,
                  coverage_pct=acc["coverage_pct"], faithfulness_pct=acc["faithfulness_pct"],
                  coverage=gate.report.coverage, faithfulness=gate.report.faithfulness,
                  critical_failures=gate.report.critical_failures, summary=gate.report.summary)

        # 4) SYNTH (only if the gate allows)
        if gate.synthesize:
            yield _ev("synthesize.start", track=track)
            audio = await ElevenLabsClient().synthesize(gate.report... , f"{patient_id}_{key}")
            yield _ev("synthesize.done", track=track, audio_url=audio)
        else:
            yield _ev("synthesize.skipped", track=track, reason=gate.report.verdict)

    # store + finalize exactly as process_discharge does today
    ...
    yield _ev("complete", payload=final_response_dict)
```

Provide an analogous `run_preop_stream(...)` (single `pre_op` track,
`generate(structured, "pre_op")`). Factor the existing store/episode/persist logic
out of the current endpoints so both the streaming and non-streaming paths reuse it
(no behavior drift). `_chart_summary` returns small safe counts only — **no PHI in
events beyond what the doctor already typed** (e.g. `{medications: 4, red_flags: 3,
has_follow_up: true, new_or_changed_meds: 2}`); send item *labels/categories*, not
raw clinical free-text dumps.

### 1b. Streaming endpoints

Add SSE endpoints next to the existing ones (keep the originals for backward
compatibility / non-JS callers):

```python
from fastapi.responses import StreamingResponse
import json

async def _sse(gen):
    try:
        async for ev in gen:
            yield f"data: {json.dumps(ev)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps(_ev('error', status='error', message=str(exc)))}\n\n"

@app.post("/api/process-discharge/stream")
async def process_discharge_stream(input_data: DischargeInput, user=Depends(get_current_user_optional)):
    gen = run_postop_stream(input_data, patient_id=_new_pid(), ctx=_ctx(user))
    return StreamingResponse(_sse(gen), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.post("/api/process-preop/stream")
async def process_preop_stream(input_data: PreOpInput, user=Depends(get_current_user_optional)):
    ...
```

Notes:
- `X-Accel-Buffering: no` + `Cache-Control: no-cache` so nginx/Railway/Cloudflare
  don't buffer the stream. If your proxy still buffers, document the dashboard
  fallback (1c).
- The terminal `complete` event carries the same dict the non-stream endpoint
  returns, so the frontend resumes its existing success path unchanged.
- Auth identical to the current endpoints.

### 1c. Graceful fallback
If grounding deps are absent, simply don't emit the `grounding.*` / use
`synthesize` directly — the UI must handle a missing Safety stage (render it as
"skipped"). If streaming fails/buffers, the frontend falls back to the existing
non-stream POST and its old overlay (keep that code path alive).

---

## 2. Frontend — the Generation Theater (`frontend/doctor.html`)

Replace the manual `#processing` overlay body with a live theater driven by the
stream. **Do not** use `EventSource` (it's GET-only); POST and read the stream:

```javascript
async function streamGeneration(path, body, onEvent) {
  const res = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": `Bearer ${authToken}` },
    body: JSON.stringify(body),
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, i); buf = buf.slice(i + 2);
      const line = frame.split("\n").find(l => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)));
    }
  }
}
```

Wire the two existing trigger handlers to call `streamGeneration("/api/process-
discharge/stream", body, applyTheaterEvent)` (and the pre-op one), feeding events
into the renderer below. On `complete`, run the existing success path (toast,
refetch, close). On `error`, show the error state (don't just leave it spinning).

---

## 3. The theater layout (what the doctor sees)

A four-stage pipeline rail, top to bottom, each stage a card that moves through
`pending → active → done` (or `blocked`). Keep it inside the existing modal; make
it feel like a control-room readout, not a toy.

```
┌──────────────────────────────────────────────────────────────┐
│  Generating materials for  Jane Doe · Post-op                  │
│                                                                │
│  ●─ 1. Reading the chart                            [✓ 0.8s]   │
│     claude-sonnet-4-6 · ehr_extract v1.0.0                     │
│     Found: 4 medications · 3 red flags · follow-up ✓           │
│                                                                │
│  ●─ 2. Writing the materials                        [active]   │
│     Diagnosis  claude-sonnet-4-6 · diagnosis_voice v1.0.0 ·#a1b2│
│     Treatment  claude-sonnet-4-6 · treatment_voice v1.0.0 ·#c3d4│
│                                                                │
│  ●─ 3. Safety review  (grounding check)             [active]   │
│     Judge: claude-sonnet-4-6 · GROUNDING_PROMPT_V 2026-05-31.1 │
│     Checking that the script includes & invents nothing:       │
│       ⏳ Stop Coumadin 5 days before        CRITICAL           │
│       ⏳ NPO after midnight                  CRITICAL           │
│       ⏳ Call if fever ≥ 100.4°F             CRITICAL           │
│       ⏳ Follow-up 6/12 with Dr. Okafor      MAJOR             │
│     Coverage  ▰▰▰▰▱ 92%      Faithfulness  ▰▰▰▰▰ 100%          │
│                                          ┌───────────────┐     │
│                                          │   ✓  PASS     │     │
│                                          └───────────────┘     │
│                                                                │
│  ●─ 4. Voicing the script                          [pending]   │
└──────────────────────────────────────────────────────────────┘
```

### Stage behavior driven by events
- **1 Reading the chart** — `extract.start` → active with the model + prompt chip;
  `extract.done` → done, show the safe summary counts.
- **2 Writing the materials** — `generate.start` (per track) → render a row per
  track with a **model chip** and a **prompt chip** (`label vX.Y.Z · #sha`, sha in
  monospace). `generate.done` → check the row. This is the "what model + prompts"
  requirement — make the chips legible, not buried.
- **3 Safety review** — the centerpiece:
  - `grounding.start` → reveal the **checklist** from `required_items`, each row =
    a humanized `text` + a **severity pill** (CRITICAL=`--danger`, MAJOR=`--warn`,
    MINOR=`--muted`), each starting as `⏳ checking`. A subtle "scanning" shimmer
    sweeps the list while active. Show the judge model + `prompt_version` line.
  - `grounding.checking` → label the phase ("Checking for omissions…", then
    "Checking for fabrications…").
  - `grounding.result` → for each checklist row, swap `⏳` to **✓ COVERED**
    (`--ok`), **⚠ PARTIAL** (`--warn`), or **✗ MISSING** (`--danger`) by matching
    `coverage[].id`; pop the icon in. Animate two meters counting up to
    `coverage_pct` and `faithfulness_pct`. Then stamp the **verdict badge**: PASS
    (`--ok`), REVIEW (`--warn`), BLOCK (`--danger`) with a short `summary`. If
    `grounding.regenerated` arrived, show a small "first draft flagged → redrafted"
    note so the save is visible, not hidden.
  - For **faithfulness**, optionally list any `UNSUPPORTED` claims under a
    "Flagged as invented/drifted" subhead in `--danger` — this is the fabrication
    catch, and showing it is the point.
- **4 Voicing** — `synthesize.start` → active; `synthesize.done` → done;
  `synthesize.skipped` → show "Held for clinician review — not voiced" in `--warn`/
  `--danger` (a BLOCK must look intentional, not broken).

Two post-op tracks: show them as **two columns/lanes** in stages 2–4 (Diagnosis |
Treatment) or stacked sub-cards; pre-op has one lane. Group every event by its
`track` field.

---

## 4. Design quality bar (non-negotiable)

- **Motion with taste, not noise.** Stage transitions: 200–300ms ease; checklist
  rows stagger-fade in ~40ms apart; verdict badge does one spring/scale pop;
  numbers count up over ~600ms. Active stage gets a soft pulsing accent ring or a
  slow scanning shimmer. Nothing bounces forever.
- **Respect `prefers-reduced-motion`** — drop shimmer/counts to instant state
  changes.
- **Palette = existing CSS variables only.** PASS/COVERED `--ok`, REVIEW/PARTIAL
  `--warn`, BLOCK/MISSING `--danger`, chrome in `--accent`/`--muted`/`--line`.
  Never color-only: pair every status with an icon + text label (✓ ⚠ ✗) for
  accessibility and color-blind users.
- **Typographic hierarchy.** Stage titles bold; model/prompt chips small,
  monospace sha, pill-shaped, low-contrast surface (`--line` border) so they read
  as metadata not headlines.
- **Density & calm.** Generous spacing (`--space`), one accent color, rounded
  `--radius`. It should look like a clinical instrument readout — confident and
  quiet — not a gamer RGB bar.
- **Honest pacing.** Stages light up on real events, never on a fake timer. If a
  stage is genuinely fast, let it be fast; don't pad. The *only* synthetic touch
  allowed is the count-up animation and the coverage shimmer, both tied to real
  results.
- **Failure is a first-class state.** A BLOCK verdict and a `synthesize.skipped`
  must render as a deliberate, explained outcome (verdict badge + reasons +
  "routed to clinician review"), with a clear primary action — not a dead spinner
  or a generic error.
- **Accessibility:** `aria-live="polite"` on the stage region so screen readers
  announce stage/verdict changes; checklist is a real `<ul>`; meters use
  `role="progressbar"` with `aria-valuenow`.
- **Resilience:** if the stream stalls > ~30s on a stage, show a soft "still
  working…" hint; if the connection drops, fall back to the non-stream POST and the
  legacy overlay so a patient is never lost.

Build the theater as a small self-contained module in `doctor.html` (a `theater`
object with `mount(container, {track, patientName})`, `apply(event)`, `finish()`,
`fail()`), plus its own scoped CSS block using the existing variables — so it's
easy to lift into the React landing app later if needed.

---

## 5. Tests / acceptance

- **Backend:** a test that drives `run_postop_stream` / `run_preop_stream` with a
  mocked judge and asserts the event sequence: `pipeline.start` → `extract.*` →
  `generate.*` (with `prompts[].version`+`sha` present) → `grounding.start` (with
  non-empty `required_items`) → `grounding.result` (verdict + both pct) →
  `synthesize.*` → `complete` (payload == non-stream response). Assert a BLOCK case
  emits `synthesize.skipped` and never calls ElevenLabs. Assert events carry **no
  raw clinical free-text** beyond the safe summary.
- **Frontend (manual acceptance):** generate one clean post-op (watch all four
  stages, two lanes, PASS) and one deliberately broken pre-op — omit a med-hold —
  and confirm the checklist shows the CRITICAL item flip to ✗, coverage drops, the
  verdict reads BLOCK, the redraft note appears, and synthesis shows "held for
  review." Confirm `prefers-reduced-motion` disables motion. Confirm a proxy that
  buffers still completes via the fallback path.

---

## Build order
1. `pipeline/streaming.py` — extract shared store/persist logic from the current
   endpoints; add `run_postop_stream` / `run_preop_stream`.
2. `/api/process-discharge/stream` + `/api/process-preop/stream` SSE endpoints.
3. `frontend/doctor.html` — `streamGeneration()` reader + the `theater` module +
   scoped CSS; wire the two trigger handlers; keep the legacy overlay as fallback.
4. Tests + the two manual acceptance runs.
