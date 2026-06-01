# Close the Grounding Gaps + Make Gating Structural (Cursor Build Prompt)

> Paste into Cursor. Two confirmed problems + one structural fix so they can't
> regress. Scope is small and surgical — the grounding checker and
> `audit_and_gate_script` already exist; this wires the **one path that escapes
> them**, closes a logging consistency gap, and adds a chokepoint so future
> synthesis calls cannot ship ungated.

---

## Background (verified against the current code)

- `pipeline/grounding_gate.py::audit_and_gate_script(*, patient_id, structured_data,
  script, track, team_store, regenerate_fn=None)` runs the grounding check,
  persists the report, and returns a gate with `.script` and `.synthesize`.
- **Pre-op IS already gated** in `main.py`: `/api/process-preop`, the legacy
  `/api/process-patient` (via `grounding_track = "pre_op" if pipeline_type ==
  "pre_op" else "post_op_treatment"`), and `_ensure_preop_voice_audio`. **Verify
  this is intact — do not duplicate the gate there.**
- **The gap:** `eligibility/pipeline.py` (the batch / eligibility onboarding
  fan-out) calls `GenerationLayer().generate(sd, pipeline_type)` and then
  `ElevenLabsClient().synthesize(...)` for **both pre_op and post_op** with **no
  grounding gate**. This batch path ships unchecked pre-op (fasting / med-hold)
  and post-op content. This is the primary fix.
- **Minor:** `pipeline/grounding_check.py::check_grounding` has an injected-client
  branch that calls `client.messages.create(...)` directly, bypassing `call_llm`
  (so that path is unlogged / unversioned).

---

## Task 1 — Gate the eligibility/batch synthesis path (the real hole)

In `backend/eligibility/pipeline.py`, in the function that does
`gen.generate(sd, pipeline_type)` then `ElevenLabsClient().synthesize(...)`
(around the `voice_script, battlecard_html = await gen.generate(sd, pipeline_type)`
line): insert the gate **between generation and synthesis**, mirroring the legacy
`/api/process-patient` flow.

```python
from pipeline.grounding_gate import audit_and_gate_script, apply_grounding_to_patient
from team_store import TeamStore   # same SQLite-backed store the rest of the app uses

_store = TeamStore()

gen = GenerationLayer()
voice_script, battlecard_html = await gen.generate(sd, pipeline_type)

track = "pre_op" if pipeline_type == "pre_op" else "post_op_treatment"

async def _regen():
    nonlocal battlecard_html
    v, b = await gen.generate(sd, pipeline_type)
    battlecard_html = b
    return v

gate = await audit_and_gate_script(
    patient_id=pid,                     # the same pid used for the audio filename
    structured_data=sd,
    script=voice_script,
    track=track,
    team_store=_store,
    regenerate_fn=_regen,
)
voice_script = gate.script

voice_audio_url = None
if gate.synthesize:                     # BLOCK -> no audio, exactly like main.py
    voice_audio_url = await ElevenLabsClient().synthesize(voice_script, f"{pid}_{audio_suffix}")

# persist verdict onto the patient blob so the batch path shows up in the
# admin Grounding Checker tab and in requires_clinician_review, like the others
apply_grounding_to_patient(patient, track, gate)
```

Notes:
- Compute `pid` **before** the gate (it's currently computed just above the
  synth call — move it up).
- Keep the existing `try/except` around `synthesize` so a TTS failure still
  degrades to "audio unavailable" rather than a hard error.
- If this fans out over many patients (batch), the gate adds one judge call per
  patient — that is intended; it is the safety check. If latency matters, gate
  inside the same `asyncio.gather` you already use for generation.

---

## Task 2 — Close the unlogged judge path

In `backend/pipeline/grounding_check.py::check_grounding`, the injected-`client`
branch (`await client.messages.create(...)`) is only used by tests. Make
production always route through `call_llm` so every judge call is logged +
versioned. Simplest fix: keep the `client` param **for tests only** and, when it
is provided, still record an `llm_call`-style note — or better, drop the raw
branch and have tests inject a fake via the `call_llm` seam instead.

Recommended change:
- Always call `call_llm(role="grounding_judge", prompt_id="grounding_judge",
  patient_id=patient_id, system=GROUNDING_JUDGE_PROMPT, messages=[...])`.
- For tests, monkeypatch `ai.llm_client.call_llm` (or pass a
  `judge_call=call_llm`-shaped callable) rather than a raw Anthropic client, so the
  logging/versioning path is exercised even under test.
- Add a `grounding_judge` entry to `PROMPT_REGISTRY` (content =
  `GROUNDING_JUDGE_PROMPT`, version `"1.0.0"`) and thread an optional `patient_id`
  param through `check_grounding(...)` and `audit_and_gate_script(...)` so judge
  calls are attributable.

---

## Task 3 — Make gating STRUCTURAL so it can't regress (the actual compliance feature)

The reason a third synthesis path slipped through is that gating lives at each
call site. Add a single chokepoint + a guard test so a new `synthesize()` of a
generated script is impossible to ship ungated.

### 3a. One gated-synthesis helper

Create `backend/pipeline/gated_synthesis.py`:

```python
from integrations.elevenlabs import ElevenLabsClient
from pipeline.grounding_gate import audit_and_gate_script, apply_grounding_to_patient

async def synthesize_script(*, patient_id, structured_data, script, track,
                            team_store, audio_id, regenerate_fn=None,
                            patient_blob=None):
    """The ONLY sanctioned way to turn a generated voice script into audio.
    Runs the grounding gate first; synthesizes only on a non-BLOCK verdict."""
    gate = await audit_and_gate_script(
        patient_id=patient_id, structured_data=structured_data, script=script,
        track=track, team_store=team_store, regenerate_fn=regenerate_fn,
    )
    if patient_blob is not None:
        apply_grounding_to_patient(patient_blob, track, gate)
    audio_url = None
    if gate.synthesize:
        audio_url = await ElevenLabsClient().synthesize(gate.script, audio_id)
    return gate, audio_url
```

Migrate the script-synthesis call sites (the two-resource post-op flow,
`/api/process-preop`, legacy `/api/process-patient`, `_ensure_preop_voice_audio`,
and the new eligibility path) to call `synthesize_script(...)` instead of calling
the gate and `ElevenLabsClient().synthesize` separately. This collapses ~5 hand-
wired gates into one audited helper.

### 3b. Regression guard test

Create `backend/tests/test_synthesis_is_gated.py`:

```python
import re, pathlib

# Call sites allowed to call ElevenLabs directly because they are NOT
# patient-education scripts (e.g. live chat replies). Keep this list tiny and
# justified — every entry is a documented exception.
ALLOWLIST = {
    "pipeline/gated_synthesis.py",        # the sanctioned chokepoint itself
    # "main.py:care_companion_chat",      # add ONLY with a written reason
}

def test_no_ungated_script_synthesis():
    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for p in root.rglob("*.py"):
        rel = str(p.relative_to(root))
        if rel.startswith("tests/") or rel in ALLOWLIST:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"ElevenLabsClient\(\)\.synthesize\(", text):
            offenders.append(rel)
    assert not offenders, (
        "Direct ElevenLabs synthesis outside the gated chokepoint "
        f"(route through pipeline/gated_synthesis.synthesize_script): {offenders}"
    )
```

This test fails the build the moment someone adds a new ungated `synthesize(...)`
of a script — turning "remember to gate" into a guarantee.

---

## Task 4 — Tests

- `eligibility/pipeline.py`: a test where the judge returns BLOCK → assert
  `synthesize` is NOT called and `voice_audio_url is None`; a PASS case → audio is
  produced. Mock the judge (`call_llm`) so it's offline.
- `check_grounding`: assert the judge call now emits an `llm_call` event with
  `prompt.prompt_id == "grounding_judge"` (proves Task 2).
- Run `test_synthesis_is_gated.py` and confirm it passes after the migration (and
  fails if you temporarily add a stray `ElevenLabsClient().synthesize(` — sanity
  check the guard works, then revert).

---

## Verification checklist (run before committing)

1. `grep -rn "ElevenLabsClient().synthesize(" backend --include=*.py | grep -v tests`
   → every remaining hit is either inside `pipeline/gated_synthesis.py` or on the
   `ALLOWLIST` with a written reason.
2. `grep -rn "client.messages.create" backend/pipeline/grounding_check.py` → gone
   (or only in a test-seam).
3. Trigger an eligibility/batch onboard with a deliberately bad pre-op note
   (omit a med-hold) → confirm a BLOCK report appears in the admin Grounding
   Checker tab and no audio was synthesized.
4. Confirm `/api/process-preop` still gates (Task 0 sanity — do not double-gate).

---

## Why this is the right shape

- **Task 1** closes the live hole: the batch path that ships pre-op fasting/med-
  hold content unchecked.
- **Task 2** makes the inspector audit itself (no blind spots in the call log).
- **Task 3** converts gating from a per-site convention into an enforced invariant
  — the compliance property a safety committee actually wants: *"no patient audio
  is produced without a recorded grounding verdict,"* provable by one test rather
  than by reviewer vigilance.
```
