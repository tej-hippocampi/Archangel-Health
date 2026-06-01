# PRD — Teach-Back Comprehension Feature (MVP)

| Field | Value |
|---|---|
| Feature | Teach-Back Comprehension |
| Document version | 1.0 (build-ready) |
| Owner | Tej Patel |
| Status | Build-ready |
| Last updated | 2026-06-01 |
| Primary user | Patient (answers teach-back); RN care coordinator (consumes the resulting tier + escalation) |
| Secondary users | NP / PA, Surgeon (read), Clinician author/reviewer of question + seed sets |
| Implementation target | Existing Python/FastAPI/SQLite backend + vanilla-JS frontend (`frontend/*.js`, `styles.css`). This PRD reuses the live codebase patterns directly; there is **no** Next.js/Prisma layer for this feature. |
| Audience | Claude Code / engineering implementers |
| Depends on | Grounding inspector (`backend/pipeline/grounding_check.py`); post-op re-tier (`backend/triage/postop/`); pre-op re-tier (`backend/triage/preop_retier/`); team store (`backend/team_store.py`); patient surfaces (`frontend/pre-op.js`, `frontend/postop.js`); generated `voice_script` + `battlecard_html` per track |
| Supersedes | None — first comprehension-check PRD |

---

## 0. Reading order and conventions

- **TIER_3 = highest risk** (preserved from the triage PRD suite). Re-tier engines write the live `Episode.tier`.
- This feature builds a teach-back step that runs **after** a patient consumes an education video + battlecard for a track, grades their free-text restatement against **their own record**, re-teaches once on a miss, and feeds the **post-loop** result into the existing re-tier engines.
- It is the **grounding-inspector machinery pointed at the patient instead of the script**: same direct-`AsyncAnthropic` call pattern, same fail-safe seams, same validation/recall discipline.
- **The three tracks are exactly the grounding tracks** — reuse the existing constant `VALID_TRACKS = {"pre_op", "post_op_diagnosis", "post_op_treatment"}` (`backend/pipeline/grounding_check.py:21`). Do not invent a new track enum.
- **Severity levels are CRITICAL and MAJOR** — matching what `build_required_items()` actually emits (`grounding_check.py`). There is no MINOR item severity in `build_required_items`; do not assume one. (MINOR appears only in the judge prompt's faithfulness vocabulary.)
- Throughout, "post-loop result" means the patient's **second and final** answer to a question, recorded after one re-teach. First-attempt misses are teaching moments and carry **zero** risk weight (see §1 and §7 — this is evidence-mandated, not a preference).

---

## 1. Goal & evidence basis

**Goal.** Confirm the patient actually *understood* their education — in their own words — rather than merely *watched* it, and turn **persistent** comprehension gaps into a modifiable-risk signal that the re-tier engines and the RN queue can act on. Replace the exposure signal ("did they view the video", already scored by `video_engagement.py`) with a comprehension signal ("can they state what to do and when to act").

**Evidence basis (stated honestly — see §13 for the bibliography and confidence grades).**

- **Teach-back is an established standard, not a settled outcome intervention.** It is AHRQ Health Literacy Universal Precautions Toolkit **Tool 5** and the IHI "Always Use Teach-Back!" practice, and is endorsed for surgical informed consent by the Joint Commission, National Quality Forum, and Leapfrog. The endorsement rests heavily on **low cost, low harm, and face validity**.
- **Readmission evidence is real but low-strength — do not overstate it.** The widely-quoted "~45% fewer 30-day readmissions" traces to a single systematic review (Dinh et al., *J Patient Safety* 2021) that pooled only **3 nonrandomized studies** (pooled OR 0.55, 95% CI 0.34–0.91 — the upper bound nearly touches 1.0). A heart-failure-specific meta-analysis found OR 0.40 (95% CI 0.17–0.94). **There is no Cochrane review and no large RCT** showing a teach-back readmission benefit, and **surgical-specific readmission evidence is thin**. Cite teach-back as *associated with* reduced readmission, never as proven to cause it.
- **The comprehension evidence is stronger and more directly relevant.** A randomized controlled ED study (Griffey et al., 2015) showed teach-back improved objective comprehension of post-discharge care, medications, self-care, and follow-up in limited-health-literacy patients; a prospective cohort (EM-TeBa, 2020) saw the share of patients leaving with a comprehension deficit fall from **49% to 11.9%**. Caveat: gains are mostly measured immediately; **retention decays** over time.
- **Patients cannot self-assess comprehension.** ~80% of patients with poor comprehension do not recognize their own deficit (Engel et al., *Ann Emerg Med* 2009); >75% of ED patients had a deficit in at least one domain. This is the core reason the feature must elicit a produced-answer signal, not a "got it?" self-report.

**Net positioning for stakeholders:** teach-back is an evidence-supported, low-harm comprehension check whose *comprehension* benefit is well-grounded and whose *readmission* benefit is plausible but weakly evidenced. We deploy it as a **comprehension-risk signal feeding existing tiering**, validate it locally (§10), and tune the weights against observed data rather than claiming a pre-validated risk instrument exists (none does — see §13).

---

## 2. Scope

**In (MVP).**

1. Three tracks, reusing `VALID_TRACKS`: `pre_op`, `post_op_diagnosis`, `post_op_treatment`.
2. Text (typed) free-text answers only.
3. Per-patient, clinically-graded, open-ended questions generated from the patient's own `structured_data` + the generated `voice_script`/`battlecard_html`.
4. One re-teach loop per question (locate the answer in the materials → re-ask once).
5. Post-loop result wired into both re-tier engines (post-op and pre-op).
6. Persistence + admin stats + grader-recall validation, mirroring the grounding inspector.

**Out (MVP).**

- Voice/spoken answers (defer).
- "Show me"/demonstration answers (defer; not feasible in a text channel — flagged as the strongest future enhancement for wound-care items, per §13).
- Multi-loop (>1) re-teach.
- Clinician authoring UI for questions (questions + seeds are authored in code/fixtures and clinician-reviewed in MVP).
- **Audio-seek-to-answer** during re-teach. The ElevenLabs integration (`backend/integrations/elevenlabs.py`) is **text-to-speech only and returns raw MP3 bytes with no word-level timings**, so `audio_seek_sec` is not computable in MVP. Re-teach highlights the battlecard section + surfaces the transcript line only. (Word-timing capture is a future enhancement.)

---

## 3. Core UX flow (the agreed design)

After the patient finishes a track's video + battlecard on the SAME page
(`/patient/{id}/pre-op` → `pre-op.js`; post-op dashboard → `postop.js`):

1. Reveal a **Teach-Back panel inline below the player** (confirmed design: inline, not split or floating). Materials stay **fully available** — do NOT blur the battlecard, do NOT lock the video.
   > **Why open-book (evidence-mandated, not a style choice).** AHRQ Tool 5 states verbatim that teach-back is *"not a test of the patient's knowledge … a test of how well you explained the concept."* Operational guidance has clinicians annotate the printed materials and have the patient locate/state the answer. Locking the materials would measure recall-under-pressure, which (a) is *not* what teach-back is, (b) harms low-literacy and post-anesthesia patients (~40% of surgical patients >60 have measurable postoperative cognitive dysfunction at discharge), and (c) pollutes the risk signal with a sedation/fog confound. This is explicitly **not** a closed-book exam.
2. Ask **2–3 open-ended questions, one at a time** ("In your own words, …"). This matches the "chunk and check / triage to the vital few" rule (IHI; the 5Ts model). Never present all questions as one wall of text.
3. Patient types an answer. Provide an **"I'm not sure"** button on every question — selecting it routes to help (care-team contact) and is recorded as a **non-answer, NOT a wrong answer**. (Non-shaming environment; "I'm not sure" is a legitimate, safe response.)
4. Grade each answer (see §5).
5. **On PASS** → advance to next question; on the last question, mark the track complete.
6. **On PARTIAL/FAIL** → enter the **locate-and-re-teach loop** (§6): scroll to + gently highlight the battlecard section containing the answer, surface the supporting transcript line as a callout, then re-ask the **same** question once. Re-teach copy frames it as the explanation's fault, not the patient's (AHRQ "Take Responsibility": *"I don't think I explained that part well — let me go over it again."*).
7. After the single re-ask, record the **post-loop** result for that item and move on. Never trap the patient in a loop.

> **UI decisions confirmed with product:** inline panel below the player; gentle highlight + scroll + transcript callout for re-teach (no modal/spotlight, no auto-replay); calm confirmation on completion, and on a persistent **red-flag** failure surface the care-team contact path (do not pass silently, do not alarm). No score is shown to the patient.

---

## 4. Question generation (`backend/pipeline/teachback_questions.py`)

Mirror the grounding inspector's module shape (constants + a pure builder + one graded async call with a test-injectable client and a fail-safe).

- Generate questions per `(patient, track)` from `structured_data`, **reusing `pipeline/grounding_check.py::build_required_items`** to enumerate the gradeable items and their severity. **Prioritize CRITICAL items**, ordered by the evidence-based harm ranking in §5.1.
- Module constants (mirroring `GROUNDING_PROMPT_V` / `GROUNDING_JUDGE_MODEL`):
  ```python
  TEACHBACK_QUESTIONS_PROMPT_V = "2026-06-01.1"
  TEACHBACK_AUTHOR_MODEL       = "claude-sonnet-4-6"   # match grounding's model choice
  ```
- Each question object:
  ```json
  {"id": "...", "track": "...", "severity": "CRITICAL|MAJOR",
   "domain": "RED_FLAG|MED|FASTING|MED_HOLD|WOUND_CARE|ACTIVITY|FOLLOWUP|MAIN_PROBLEM",
   "form": "OPEN_ENDED|SCENARIO|WHY",
   "question": "If you got home and your incision was red, hot, and draining — who would you call, and when would you go to the ER?",
   "expected": "<the patient-specific correct answer, derived from structured_data>",
   "source_quote": "<verbatim line from the generated voice_script supporting it>",
   "battlecard_anchor": "<id/selector of the battlecard element with the answer>"}
  ```
- `expected` / `source_quote` / `battlecard_anchor` are computed at generation time so the re-teach loop knows exactly where to send the patient.
- Generate via a **direct `AsyncAnthropic` call** (see §5 — there is no `call_llm` wrapper in this codebase). Ground strictly in `structured_data` + the generated `voice_script` / `battlecard_html`; **no invented facts** (same rule as the grounding inspector — validated in §10/§11).
- **Inject anchor ids into the battlecard HTML at generation time** so the frontend can highlight a specific section. The frontend renders the battlecard via `container.innerHTML = battlecard_html` (`pre-op.js` ≈ line 1027; analogous in `postop.js`), so injected `id="tb-anchor-…"` attributes are directly queryable client-side.

### 4.1 Question design — evidence-backed forms and priorities

The question *content and form* is the clinical core. Use these evidence-based forms (AHRQ Tool 5; IHI; Engel 2009/2012; 5Ts 2020):

- **Open-ended ("in your own words")** is the default. Never yes/no, never "Do you understand?" — patients say "yes" regardless, and most cannot detect their own gaps.
- **Scenario ("what would you do if…")** for warning-sign/red-flag and activity items — this forces the patient to produce the *action path* (who to call, when to go to the ER), which is the high-harm part that plain recall under-tests.
- **"Why is this important"** for medications and pre-op medication-hold — stating the rationale is adherence-supportive and maps to Ask Me 3. *(Moderate-confidence: rationale-inclusion improves adherence in education meta-analyses (d≈0.18), but no head-to-head trial isolates "ask the why" vs "ask the what." Keep WHY prompts — low cost, framework-aligned — without over-claiming a comprehension benefit.)*
- **Demonstration ("show me")** is the ideal form for wound care but is **out of scope** for a text MVP (see §2); approximate with an open-ended "walk me through" prompt.

**Per-track priority (drives which CRITICAL items become questions first):**

| Track | Top-priority domain (ask first) | Form | Then |
|---|---|---|---|
| `post_op_treatment` (red-flag track) | **Warning signs + exact action** | SCENARIO | wound care (OE), activity (SCEN) |
| `post_op_diagnosis` | New/changed **medications** (name, dose, frequency, purpose) | OE + WHY | main problem (OE), follow-up (OE) |
| `pre_op` | **Fasting (NPO)** and **medication-hold** | OE + SCEN / OE + WHY | arrival logistics (OE), consent/main risks (OE) |

Rationale for prioritization (Engel 2012 deficit prevalence × consequence): home/self-care (~80% deficit) and return/warning-sign (~79% deficit) are both the **most-misunderstood and highest-consequence** domains, which is exactly why warning-signs lead the red-flag track. Medications have a lower deficit rate (~22%) but high consequence, so they lead the diagnosis track but sit below red-flags in weighting (§7).

---

## 5. Grading (`backend/pipeline/teachback_grade.py` — mirror `grounding_check.py`)

- Module constants:
  ```python
  TEACHBACK_GRADE_PROMPT_V = "2026-06-01.1"
  TEACHBACK_JUDGE_MODEL    = "claude-sonnet-4-6"
  ```
- `class TeachBackGrade(BaseModel)`: `question_id: str`, `status: Literal["PASS","PARTIAL","FAIL"]`, `missing: list[str]`, `evidence: str`, `severity: str`, `domain: str`, `model: str = TEACHBACK_JUDGE_MODEL`, `prompt_version: str = TEACHBACK_GRADE_PROMPT_V`. (Same field discipline as `GroundingReport`.)
- ```python
  async def grade_answer(
      question: dict, patient_answer: str, structured_data: dict,
      *, patient_id: str | None = None, client: AsyncAnthropic | None = None,
  ) -> TeachBackGrade:
  ```
  **Same shape and seams as `check_grounding`** (`grounding_check.py:341`): a test-injectable `client`, and a **fail-safe** — if `ANTHROPIC_API_KEY` is missing or the model output fails to parse (`JSONDecodeError`/`ValidationError`/etc.), return a deterministic safe result via a `_fail_safe_grade(...)` helper. **Fail-safe direction for grading is `PARTIAL` (route to re-teach), never silent `PASS`** — mirroring how grounding fails safe to `BLOCK`. A grader outage must never auto-pass a patient on a red flag.
- Judge system prompt `TEACHBACK_JUDGE_PROMPT` (module constant). Key rules:
  - Grade the answer against `expected` (the patient's **OWN** record) — NOT a generic textbook answer.
  - Match on **meaning, not wording**; reading from the battlecard verbatim still **PASSES** (finding the right info is the skill, open-book by design).
  - `PASS` = conveys the correct action + (for safety items) the critical specific (the dose, or the action to take on a red flag). `PARTIAL` = right topic, missing a critical specific. `FAIL` = wrong or absent.
  - `"I'm not sure"` / empty is a **non-answer**, recorded separately — not graded `FAIL`.
  - Return strict JSON only.
- **LLM access pattern (corrected to match the codebase).** This repo has **no `call_llm` wrapper and no `ai/model_config.py`**. Mirror `check_grounding` exactly:
  ```python
  client = client or (AsyncAnthropic(api_key=key) if (key := os.getenv("ANTHROPIC_API_KEY")) else None)
  if client is None:
      return _fail_safe_grade(question)          # PARTIAL
  resp = await client.messages.create(
      model=TEACHBACK_JUDGE_MODEL, max_tokens=800, temperature=0,
      system=TEACHBACK_JUDGE_PROMPT,
      messages=[{"role": "user", "content": user_msg}],
  )
  ```
- **Prompt registration (corrected).** There is no SHA/`prompt_meta` machinery. Register the two new prompts in `backend/prompts/registry.py::PROMPT_REGISTRY` using its **actual schema** (`label`, `content`, `file`, `variable`, `type`) so they appear in the internal Prompt Lab, and keep the `*_PROMPT_V` version strings as module constants (the real versioning mechanism, e.g. `GROUNDING_PROMPT_V`):
  ```python
  "teachback_author": {"label": "Teach-Back — Question Author", "content": TEACHBACK_QUESTIONS_PROMPT,
                        "file": "backend/pipeline/teachback_questions.py", "variable": "TEACHBACK_QUESTIONS_PROMPT", "type": "teachback"},
  "teachback_judge":  {"label": "Teach-Back — Answer Judge", "content": TEACHBACK_JUDGE_PROMPT,
                        "file": "backend/pipeline/teachback_grade.py", "variable": "TEACHBACK_JUDGE_PROMPT", "type": "teachback"},
  ```

---

## 6. Locate-and-re-teach loop

On the **first** PARTIAL/FAIL for a question, the answer endpoint returns:
```json
{"status": "FAIL", "retry": true,
 "locate": {"battlecard_anchor": "tb-anchor-redflag-fever",
            "transcript_quote": "Call the clinic right away if your temperature goes above 101°F.",
            "audio_seek_sec": null}}
```
- Frontend: scroll to + apply `.teachback-highlight` to `battlecard_anchor`, render `transcript_quote` as a callout, then re-enable the input and re-ask the same question once. The **second submission is final** for that item.
- `audio_seek_sec` is **always `null` in MVP** (no word timings available — §2). The field is kept in the contract so audio-seek can be added later without a breaking change; the frontend treats `null` as "highlight + show the line only".
- Re-teach is **gentle** (confirmed): scroll + soft highlight + callout. No modal, no dimming, no forced audio replay.

---

## 7. Risk-engine integration (post-loop only)

**Only the post-loop result counts toward risk.** A first-attempt miss is a teaching moment, never a risk flag. This is mandated by both teach-back doctrine (AHRQ/IHI: re-explain and re-check "until correct"; the miss is attributed to the *explanation*) and mastery-learning pedagogy (formative checks are ungraded; only failure to reach mastery *after* correctives is consequential).

### 7.1 Weighting rationale (the evidence-driven part you asked to validate)

The relative weights below are anchored to the medical literature on **which comprehension failures cause the most harm**, then fitted to each engine's existing weight scale:

- **Red-flag / warning-sign failure is the single highest-priority signal.** Warning-sign recognition is *both* the most-misunderstood domain (~79% deficit, Engel 2012) *and* the highest-consequence (a patient who cannot state when/whom to call presents late → sepsis, hemorrhage, missed complication). It therefore outranks medication comprehension. **Decision: a persistent (post-loop) red-flag failure is modeled as a HARD ESCALATOR**, not a soft contributor — see §7.2 for why this is the architecturally-correct way to guarantee the human handoff the original PRD asked for.
- **Medication failure is high but ranks below red-flags** (lower deficit ~22%, and partially covered already by the engine's `MED_ADHERENCE_*` signals). → soft `+2`, matching `MED_ADHERENCE_LOW`.
- **Other CRITICAL failures** (wound care, activity, post-op fasting recall) → soft `+2`.
- **Not completing teach-back** is an engagement signal (comprehension never confirmed) → soft `+1`, consistent with the existing `*_VIDEO_NOT_VIEWED_*` weights. Note teach-back failure is **additive to**, not redundant with, video-not-viewed: the latter measures exposure, the former comprehension.
- **Passing all** is a positive comprehension signal: post-op records it **audit-only (weight 0)** (the post-op engine is positive-only, mirroring `RED_FLAG_VIDEO_VIEWED_BY_D2`); pre-op gives it a **−1 credit**, consistent with how the pre-op engine already rewards completion (`ENGAGEMENT_FULLY_COMPLETE_BY_T_24 = −1`).

> **Operational safety note on the hard escalator.** Because the red-flag deficit baseline is high (~79% pre-teaching), making *post-loop* red-flag failure a hard escalator is the safety-conservative default but must be **monitored**: watch the post-loop red-flag failure rate via `/admin/teachback/stats` and grader recall (§10). If real-world volume is unmanageable, the fallback is a soft `+3` (the "RED" band, = `CHECKIN_TIER_RED`) plus a dedicated escalation hook. We default to the hard escalator because a patient who *still* cannot state their red flags **after re-teaching** is a genuine safety risk.

### 7.2 Post-op (`triage/postop/types.py`, `delta.py`, `tuning.py`, `hard.py`, `apply.py`)

- **Add to `PostOpReTierInput`:** `teachback_completed: bool`, `teachback_failed_critical: bool` (any non-red-flag CRITICAL item still PARTIAL/FAIL post-loop), `teachback_failed_red_flag: bool`, `teachback_failed_med: bool`, `teachback_not_completed_by_d5: bool`.
  > **Timing corrected:** the original PRD used `…_by_d3`. Align the not-completed gate to the existing **video windows** instead — red-flag video is `…_NOT_VIEWED_BY_D5` and diag/treat is `…_BY_D14`. Teach-back unlocks *after* video consumption, so a D3 gate would fire before the red-flag video is even expected. Use **D5** (the red-flag window). 
- **Wire reasons in `delta.py`** next to the existing video-engagement block (after the `RED_FLAG_VIDEO_*` / `DIAGNOSIS_TREATMENT_VIDEO_*` reasons, ≈ lines 111–125), via `_pos_reason(...)` and `_audit_reason(...)`.
- **Add to `POSTOP_POSITIVE_WEIGHTS` (`tuning.py`):**
  ```
  TEACHBACK_FAILED_MED_POSTLOOP        2     # == MED_ADHERENCE_LOW
  TEACHBACK_FAILED_CRITICAL_POSTLOOP   2
  TEACHBACK_NOT_COMPLETED_BY_D5        1     # == DIAGNOSIS_TREATMENT_VIDEO_NOT_VIEWED_BY_D14 band
  TEACHBACK_PASSED_ALL                 audit-only (use _audit_reason, weight 0)
  ```
  Bump `TUNING_VERSION` (currently `1`) and `MODEL_VERSION` (`postop-retier@1.0.0`). The `POSTOP_DELTA_CAP = 12` is unchanged; these contributors live under it.
- **Add the red-flag failure as a HARD escalator** in `HARD_ESCALATORS` (`tuning.py`) + evaluate it in `triage/postop/hard.py::evaluate_postop_hard_escalators`:
  ```
  {"code": "TEACHBACK_FAILED_RED_FLAG_POSTLOOP",
   "label": "Patient cannot state red flags / when to seek emergency care after re-teaching",
   "source": "Teach-back (post-loop)"}
  ```
  Any one hard escalator ⇒ proposed tier TIER_3. The existing `apply.py::_maybe_raise_escalation` (fires on `hard_escalator_fired or tier_after == "TIER_3"`) then creates the escalation row and routes it to a human — **no new escalation plumbing needed**.
  > **Correction:** the original PRD referenced `_classify_and_create_escalation` (that is the chat/intake path in `main.py`). The re-tier engines escalate through `_maybe_raise_escalation` in their own `apply.py`; routing a red-flag failure through it requires modeling it as a hard escalator (or pushing tier to 3), which §7.2 does.

### 7.3 Pre-op (`triage/preop_retier/types.py`, `delta.py`, `tuning.py`, `hard.py`, `apply.py`)

The pre-op engine uses **signed** weights (`WEIGHTS`), `SOFT_CAP = 12`, `STICKY_HARD_GUARD = True`, and its own `HARD_ESCALATORS`. Add the analogous fields and wire them in this engine's `types.py`/`delta.py`/`tuning.py`/`hard.py`:

- **Medication-hold post-loop failure → HARD escalator.** Most error-prone pre-op item; failure = bleeding/thrombosis (anticoagulants), hypo/hyperglycemia, or day-of cancellation. Add to `HARD_ESCALATORS` + `hard.py`:
  ```
  {"code": "TEACHBACK_FAILED_MED_HOLD_POSTLOOP",
   "label": "Patient cannot state which meds to hold before surgery after re-teaching"}
  ```
- **Fasting (NPO) post-loop failure → `+4`** (the top soft band, == `INTAKE_NOT_COMPLETE_BY_T_24`). Aspiration + case-cancellation risk. *(May be promoted to a hard escalator after observing real rates — same monitoring caveat as §7.1.)*
- **Other CRITICAL post-loop failure → `+2`.**
- **Pre-op teach-back not completed by T-24 → `+2`** (== `VIDEO_NOT_VIEWED_BY_T_24`).
- **Pre-op teach-back passed all → `−1`** (== `ENGAGEMENT_FULLY_COMPLETE_BY_T_24`; rewards confirmed comprehension).

Bump pre-op `TUNING_VERSION` + `MODEL_VERSION` (`preop-retier@1.0.0`). Escalation flows through `triage/preop_retier/apply.py::_maybe_raise_escalation`, identical pattern to post-op.

---

## 8. Persistence & APIs

### 8.1 Persistence (`team_store.py`)

Mirror the grounding-report methods (`save_grounding_report` / `list_grounding_reports` / `grounding_summary_stats`, `team_store.py` ≈ 3026–3155) with a new `teachback_sessions` table and:
- `save_teachback_session(*, patient_id, track, questions, results, completed, prompt_version, model) -> int`
- `list_teachback_sessions(*, limit=100, track=None, since=None) -> list[dict]`
- `teachback_summary_stats(*, window_days=30) -> dict` (pass/partial/fail counts and rates, grouped by track + domain).

**Event logging (corrected).** There is **no `llm_call` event type and no `LLM_LOG_RAW` env flag** in this codebase. Use the existing generic `team_store.log_event(patient_id, event_type, payload)` to emit one `teachback_result` event per completed track (payload: counts by status/severity/domain, post-loop summary — **no raw answer text**).

**PHI.** Grading runs through `AsyncAnthropic`. Raw patient answers are persisted only in the `teachback_sessions` table (needed for clinician review and grader auditing), **never** in `event_logs` payloads. If a redaction/opt-out toggle is wanted, it must be **added** (mark as new work — it does not exist today); do not reference a non-existent flag.

### 8.2 Endpoints

Patient-facing endpoints follow the existing patient-route convention used by post-op (`/api/episodes/{patient_id}/postop/video-event`); admin endpoints mount under the `/admin` prefix to match `routers/admin.py`. Add a new `routers/teachback.py` and `app.include_router(teachback_router)` in `main.py` (alongside the other `include_router` calls ≈ lines 5051–5060).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/episodes/{id}/teachback/{track}/start` | Generate/return questions for the track |
| POST | `/api/episodes/{id}/teachback/{track}/answer` | Submit one answer → grade → PASS or `retry` w/ locate hints |
| GET  | `/api/episodes/{id}/teachback/{track}` | Session state / results |
| GET  | `/admin/teachback/stats` | Pass/fail rates (mirror `/admin/grounding/stats`) |
| GET  | `/admin/teachback/grader-recall` | Grader validation metrics (mirror `/admin/grounding/inspector-recall`, see §10) |

**Gating.** A track's teach-back `start` only unlocks **after that track's video has been consumed.** Reuse the **server-side completion signal already computed** for the engagement scorer — the same derivation feeding `red_flag_video_viewed_by_d5` / `diag_treat_video_viewed_by_d5` (post-op) and the pre-op `VideoEngagement`/`preop_video_watched` view events.
> **Correction:** the frontend emits `PLAYED` (post-op, per `video_kind`) and `preop_video_watched` (pre-op) events — **there is no `postop_video_completed` event**; completion is inferred server-side from session duration. Gate on that inferred signal, not on a nonexistent completion event.

---

## 9. Frontend (`frontend/pre-op.js`, `frontend/postop.js`, `styles.css`)

- **Inline Teach-Back panel component:** question text, free-text input, Submit, "I'm not sure" button, progress ("2 of 3"). One question visible at a time.
- **Battlecard highlight:** add `.teachback-highlight` CSS; on `retry`, `querySelector` the returned `battlecard_anchor` within the injected battlecard HTML, `scrollIntoView`, apply the highlight, and render the `transcript_quote` callout. (`audio_seek_sec` is `null` in MVP → no audio action.)
- **Materials remain interactive throughout** — no blur, no lock (open-book; §3).
- **On track complete:** calm confirmation; **no score shown** to the patient.
- **On red-flag post-loop failure:** surface the care-team contact path with non-alarming copy (do not pass silently). This is the patient-facing complement to the §7.2 escalation.
- **Copy follows AHRQ framing:** prompts open with "I want to make sure I explained this clearly — in your own words, …"; re-teach copy takes responsibility ("I don't think I explained that part well…").

---

## 10. Validation — make the grader "clinically graded"

Mirror `tests/test_grounding_validation.py` + `tests/fixtures/grounding/seed_cases.py`:

- `tests/fixtures/teachback/seed_answers.py`: per question, hand-author labeled patient answers — `CORRECT`, `PARTIAL` (right topic, missing the dose/action), `WRONG`, and `EMPTY/"not sure"`. Make them realistic and subtle (paraphrases, near-misses, plausible-but-wrong doses) — crude wrongs flatter the grader. Mock the Anthropic client via `AsyncMock` with `client.messages.create` returning a `MagicMock` whose `.content[0].text` is the JSON verdict (the exact pattern in `seed_cases.py` ≈ lines 87–93).
- `tests/test_teachback_grader.py`: run `grade_answer` over the labeled set; compute **per-domain/per-severity recall** (must catch real FAILs) and **false-fail rate** on CORRECT answers (don't punish patients who got it right), using the same scoring approach as `test_grounding_validation.py::_score_results` (recall = caught/seeded; FPR = 1 − clean-caught/clean-seeded). **Assert recall == 1.0 on CRITICAL FAILs (red flags, meds)**; false-fail rate ≤ a ceiling (e.g. 0.10).
- Surface both numbers at `/admin/teachback/grader-recall`.
- **Two numbers to track:** live teach-back pass/fail rates (patient comprehension) and grader recall on seeds (whether those rates are trustworthy).

> The questions and the labeled seed answers are the clinical-judgment core and **must be authored/reviewed by a clinician** — the UI and grader are plumbing. No validated instrument maps a single teaching-episode teach-back response to a clinical risk tier (the EM-TeBa 1–4 scale is a research rubric; NVS/BHLS measure literacy capacity). This feature is therefore **novel and locally validated**, not pre-validated — say so to stakeholders.

---

## 11. Tests checklist

- Question generation grounds only in `structured_data` + generated script/battlecard (no invented meds/doses).
- Grader: PASS on correct paraphrase; PASS on verbatim battlecard read (open-book); PARTIAL on missing dose/action; FAIL on wrong; "not sure"/empty recorded as a non-answer (not FAIL).
- Loop returns locate hints (`battlecard_anchor` + `transcript_quote`, `audio_seek_sec: null`) on first miss; second submission is final.
- Risk engine: first-attempt miss adds 0 weight; post-loop med/critical failure adds the configured `+2`; **post-op red-flag post-loop failure fires the hard escalator → TIER_3 → `_maybe_raise_escalation` creates the escalation**; pre-op med-hold post-loop failure fires its hard escalator; pre-op pass-all applies `−1`.
- Not-completed gate fires at D5 (post-op) / T-24 (pre-op), not before the video window.
- Pre-op + post-op tracks both wired; `TUNING_VERSION`/`MODEL_VERSION` bumped in both.
- Grader fail-safe: missing API key / unparseable JSON ⇒ `PARTIAL` (route to re-teach), never silent PASS.

---

## 12. Non-goals / guardrails

- **No closed-book mechanic.** No blur, no video lock (evidence-mandated; §3).
- **No voice and no "show me" input in MVP** (§2).
- **No `audio_seek_sec` in MVP** — ElevenLabs has no word timings (§2/§6).
- **Never silently "correct and move on" on a persistent red-flag failure** — it fires a hard escalator and surfaces the care-team path (§7.2, §9).
- **Only post-loop results carry risk weight**; first-attempt misses carry zero (§7).
- **No reference to `call_llm`, `ai/model_config.py`, prompt SHAs, `llm_call` events, `LLM_LOG_RAW`, or a `postop_video_completed` event** — none exist in this codebase. Use the corrected seams documented in §4–§8.
- **PHI:** raw answers live only in `teachback_sessions`, never in `event_logs` payloads (§8.1).

---

## 13. Evidence appendix (bibliography & confidence grades)

**High confidence**
- **AHRQ Health Literacy Universal Precautions Toolkit, Tool 5 — "Use the Teach-Back Method."** Verbatim: teach-back is *"not a test of the patient's knowledge … a test of how well you explained the concept."* Basis for open-book design, open-ended forms, clinician-responsibility framing. https://www.ahrq.gov/health-literacy/improve/precautions/tool5.html
- **IHI "Always Use Teach-Back!" + 10 Elements of Competence.** Re-explain-and-recheck loop; "chunk and check"; non-shaming environment. https://www.ihi.org/library/tools/always-use-teach-back
- **Ask Me 3 (IHI/NPSF).** Must-understand items: main problem / what to do / why it matters. https://www.ihi.org/library/tools/ask-me-3-good-questions-your-good-health
- **Project RED (Boston University) / AHRQ RED Toolkit.** Discharge "must understand" content list (meds incl. purpose, follow-up, pending tests, what-to-do-if-a-problem-arises + who to contact, activity). https://www.bu.edu/fammed/projectred/
- **Engel KG et al., *Ann Emerg Med* 2009** — ~80% of patients with poor comprehension don't recognize it; >75% deficient in ≥1 domain. (Rationale for produced-answer over self-report.) https://pubmed.ncbi.nlm.nih.gov/18619710/
- **Engel KG et al., *Acad Emerg Med* 2012** — domain deficit prevalence: home/self-care ~80%, warning-sign/return ~79%, follow-up ~39%, meds ~22%, diagnosis ~14%. (Anchors §4.1 priority + §7.1 weighting.) https://onlinelibrary.wiley.com/doi/10.1111/j.1553-2712.2012.01425.x
- **Griffey RT et al., 2015 (RCT, PMC4659395)** — teach-back improved objective comprehension in limited-health-literacy ED patients (no change in *perceived* comprehension/satisfaction). https://pmc.ncbi.nlm.nih.gov/articles/PMC4659395/
- **EM-TeBa, *Int J Emerg Med* 2020 (PMC7513274)** — comprehension deficit fell 49% → 11.9%; 4-level (1–4) comprehension rubric. https://pmc.ncbi.nlm.nih.gov/articles/PMC7513274/
- **Anderson KM et al., "5Ts for Teach-Back," *HLRP* 2020 (PMC7156258)** — Triage to 1–3 topics; "Take Responsibility" framing; "Try Again" re-teach. https://pubmed.ncbi.nlm.nih.gov/32293689/
- **Berkman ND et al., *Ann Intern Med* 2011** — low health literacy ↔ more hospitalizations, ED use, worse medication use, higher mortality (literacy as an outcome-linked risk dimension). https://pubmed.ncbi.nlm.nih.gov/21768583/

**Moderate / low strength (cited honestly, not over-claimed)**
- **Dinh HTT et al., *J Patient Safety* 2021** — discharge teach-back vs usual care, 30-day readmission pooled **OR 0.55 (95% CI 0.34–0.91)** from **only 3 nonrandomized studies**. Source of the "~45%" figure; low strength. https://pubmed.ncbi.nlm.nih.gov/?term=Dinh+teach-back+30-day+readmission
- **Oh EG et al., *Patient Educ Couns* 2023** — HF-specific readmission **OR 0.40 (95% CI 0.17–0.94)**; authors call for more rigorous trials. https://pubmed.ncbi.nlm.nih.gov/36411152/
- **Talevski J et al., *PLOS ONE* 2020** — teach-back improves knowledge/self-care, but outcomes mostly measured immediately; durability unproven. https://pubmed.ncbi.nlm.nih.gov/32287296/
- **"Teach-back improves surgical informed consent," *Patient Safety in Surgery* 2022** — proof-of-concept (standardized patients). https://link.springer.com/article/10.1186/s13037-022-00342-9
- **Medication-adherence education meta-analysis (verbal education d≈0.18)** — supports including rationale ("why"), but no head-to-head "why vs what" trial → WHY prompt is moderate-confidence. https://www.sciencedirect.com/science/article/abs/pii/S0147956320300534
- **POCD prevalence (~40% >60 at discharge)** — supports open-book / pre-op-timed teaching to avoid sedation/fog confound. (BJA Education review.)

**Documented evidence gaps (be transparent with stakeholders)**
- No Cochrane review and no large RCT for teach-back → readmission; surgical-specific readmission evidence is thin.
- No authoritative numeric cap on re-teach loops and no measured patient-distress data — the **single** re-teach loop is a reasoned default, not an evidence-derived number.
- No deployed, validated instrument maps a single teach-back episode to a clinical risk tier — this feature's risk weighting is **locally validated and tuned**, not pre-validated.
