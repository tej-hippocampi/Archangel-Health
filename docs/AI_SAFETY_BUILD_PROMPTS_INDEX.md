# AI Safety & Compliance — Build Prompts Index

This directory contains the full set of Cursor/Claude-Code build prompts that
stand up Archangel Health's **AI safety & compliance** layer: every Claude call
is version-pinned and logged, every generated patient-education script is audited
for omissions and fabrications before it is voiced, and the whole thing is visible
and provable in the admin portal.

Read this file first. It gives you the **what**, the **why**, the **build order**,
the **dependencies between prompts**, and the **current implementation status** so
you don't rebuild something that already exists.

---

## The four prompts

| # | File | What it builds | Status (as of this branch) |
|---|---|---|---|
| 1 | `model_centralization_build_prompt.md` | One model registry + per-role env overrides; a wrapper (`call_llm`/`call_llm_sync`) that logs model + prompt version + content `sha` + tokens + latency on every Claude call; PHI gating behind `LLM_LOG_RAW`. | ✅ **Built & migrated.** Every Claude call site routes through the wrapper (correctly excludes `twilio_client.py` — that's SMS). Versioning test exists. |
| 2 | `grounding_check_build_prompt.md` | The clinical-safety inspector: deterministic required-items checklist + LLM-as-judge coverage/faithfulness audit + verdict (PASS/REVIEW/BLOCK) + the seeded "razor-blade" harness that proves the inspector catches dangerous near-misses. | ✅ **Built & enforcing.** `audit_and_gate_script` gates ElevenLabs synthesis in the live flow. 462-line seed harness exists. |
| 3 | `admin_ai_compliance_build_prompt.md` | The **"AI Security & Compliance"** admin section: Grounding Checker tab (PASS/REVIEW/BLOCK, per-script coverage/omission/fabrication detail) + AI Call Log tab (model + prompt version/sha + token spend + latency + the prompt-version provenance filter). | 🟡 **Partial.** Some grounding endpoints exist; the AI Call Log query layer + the static `frontend/admin.html` tabs are the remaining work. |
| 4 | `grounding_gate_chokepoint_build_prompt.md` | Closes the one synthesis path that escaped the gate (`eligibility/pipeline.py` batch onboarding), fixes the unlogged judge fallback, and makes gating a **structural invariant** — a single `synthesize_script` chokepoint + a guard test so no future code can voice a script ungated. Includes the `pid` ordering fix, bounded judge concurrency, and a rate-limit tuning guide. | 🔴 **Not yet built.** This is the next thing to run. |

---

## Recommended build order

The prompts are written to be run in this order. Later prompts assume the earlier
ones are in place.

```
  (1) model_centralization ──► (2) grounding_check ──► (4) gate_chokepoint
            │                          │                        │
            └──────────────┬──────────┘                        │
                           ▼                                    │
                  (3) admin_ai_compliance ◄─────────────────────┘
                  (reads what 1, 2, 4 produce)
```

1. **`model_centralization`** — foundation. Everything else logs through
   `call_llm`/`call_llm_sync` and fingerprints prompts via the registry.
   *(Already done — verify, don't rebuild.)*
2. **`grounding_check`** — the inspector + gate + seed harness. Depends on the
   `call_llm` wrapper and the prompt registry from #1.
   *(Already done — verify, don't rebuild.)*
3. **`grounding_gate_chokepoint`** (file #4) — run this **before** finishing the
   admin UI, because it makes the batch/eligibility path actually produce grounding
   reports and routes all synthesis through one audited chokepoint. Without it, the
   admin Grounding Checker tab is missing the batch-onboarded patients.
4. **`admin_ai_compliance`** (file #3) — last, because it's the read/visibility
   layer over the data the other three produce (grounding reports + `llm_call`
   events). Build it once there's real data to show.

> Net: the only **unbuilt** work is files **#4 then #3**. Files #1 and #2 are done;
> open them only to confirm nothing regressed.

---

## How the pieces fit (the one-paragraph mental model)

A patient's EHR notes are extracted and turned into a voice script by the
generator. Before that script is ever synthesized into audio, the **grounding
inspector** (#2) audits it against the structured source data — did it include
every critical item (med holds, fasting, red flags, follow-up) and did it invent
nothing (no wrong dose, no fabricated doctor, no drifted fever cutoff)? The verdict
**gates synthesis**: PASS ships, REVIEW needs a clinician, BLOCK never reaches the
patient. Every model call that participates — generation, extraction, the judge
itself — is logged with its **model + prompt fingerprint** (#1), so any output is
reproducible and any silent prompt edit is detectable. The **chokepoint** (#4)
guarantees there is no synthesis path that skips the gate. The **admin section**
(#3) makes all of it visible: which scripts passed/blocked and why, and the full
provenance trail of every Claude call.

---

## The two numbers this whole system exists to report

1. **Live BLOCK / REVIEW rate** (from the grounding audit logs) — how often the
   generator produces unsafe content. The sales number.
2. **Inspector recall on the seeded razor-blade harness** — whether #1 is
   trustworthy (a lazy inspector also has a low block rate). The number that makes
   #1 credible.

Both are surfaced in the admin Grounding Checker tab (#3). Track them from day one.

---

## Dependencies & invariants to preserve

- **Single model source:** all model ids resolve from `backend/ai/model_config.py`.
  The grep-guard test fails the build if a `claude-` literal appears anywhere else.
- **Single logging seam:** all Claude calls go through `call_llm`/`call_llm_sync`.
  Audit logging must never break the call it logs (wrapped in a guard).
- **Single synthesis chokepoint (after #4):** all script→audio goes through
  `pipeline/gated_synthesis.synthesize_script`; a guard test fails the build on any
  direct `ElevenLabsClient().synthesize(` of a script outside it.
- **Fail-safe:** an inspector that can't verify a script returns BLOCK, not PASS.
- **Determinism:** judge pinned to `claude-sonnet-4-6`, `temperature=0`;
  `GROUNDING_PROMPT_V` + `APP_AI_CONFIG_VERSION` logged with every report.
- **Behavior preservation:** the model refactor did not change temperatures where
  call sites never set one. Don't silently pin them.

---

## Verifying the "already built" prompts (#1, #2) before running #3/#4

Quick checks so you trust the status table above:

```bash
# (1) every model id comes from model_config; nothing hardcoded elsewhere
grep -rEn "claude-(sonnet|opus|haiku)" backend --include=*.py | grep -v "ai/model_config.py" | grep -v tests

# (1) all Claude calls go through the wrapper (twilio is SMS — expected to be absent)
grep -rn "messages.create(" backend --include=*.py | grep -v "ai/llm_client.py" | grep -v tests

# (2) the gate is wired into the live flow
grep -rn "audit_and_gate_script" backend/main.py

# (2) seed harness present
ls backend/tests/fixtures/grounding/seed_cases.py

# run the safety tests
pytest backend/tests/test_llm_versioning.py backend/tests/test_grounding_check.py -q
```

---

## Then run, in order

1. **`grounding_gate_chokepoint_build_prompt.md`** — close the eligibility gap, fix
   the unlogged judge path, add the `synthesize_script` chokepoint + guard test,
   apply the `pid` fix + bounded judge concurrency.
2. **`admin_ai_compliance_build_prompt.md`** — add the AI Call Log query methods +
   the admin endpoints + the two tabs in `frontend/admin.html`.

After both: open the admin portal → **AI Security & Compliance**, confirm the
Grounding Checker shows PASS/REVIEW/BLOCK (including batch-onboarded patients) and
the AI Call Log shows calls filterable by prompt version. That's the deliverable.
