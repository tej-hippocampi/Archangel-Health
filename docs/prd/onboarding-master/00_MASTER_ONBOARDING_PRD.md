# MASTER PRD — Physician onboarding: from "Open my account" to an approvable examination

One document to hand Claude Code. It orders three PRDs (A, B, C — in `prds/`) into
five phases, states what was audited, and gives the exact prompt. Verified against
`Archangel-Health-main (36)`. Evidence folders from the original investigation are in
`evidence/` and their diagnostic scripts must be converted into production tests, not
discarded.

---

## 0 · Audit of the attached PRDs (B and C)

| PRD | Verdict | Notes |
|---|---|---|
| **B — Form stability & UX** | **Correct, P0, small.** Root cause is real and reproduced by a controlled JSDOM experiment (8/29 → 26/29 checks by one change): `Group` was a component **defined inside** `Step5Credentials`, so every keystroke re-created a new component type and React remounted the whole subtree — focus lost after one character, accordion state reset, chip drafts wiped. Three residuals (missing programmatic label on the mobile field; blank board row counted as filled because the `active=true` default read as "content"; array-index keys on repeated rows) are also verified. | All 11 anchors resolved in zip (36). **One correction found on implementation:** the index-key sites are THREE, not two — board, fellowship and residency (`landing/src/app/components/onboarding/steps.tsx:2570→2703`, `:2637→2774`, `:2687→2826`); PRD B P1-A names all three groups, so only this summary was short. Shipped in Phase 1 (the hoist: `landing/src/app/components/onboarding/steps.tsx:2260→2268`) and Phase 3 (the residuals). `ReviewGroup` now lives at module scope in `landing/src/app/components/onboarding/steps.tsx:2268`. |
| **C — CV extraction → review** | **Correct diagnoses, but large.** The null→false board-validity bug is real: `applyCvParse` at `landing/src/app/components/OnboardingWizard.tsx:345` wrote `active: false` at two sites. The test that protected it is replaced by `test_a_certification_arrives_unanswered_rather_than_answered_for_them` at `backend/tests/test_cv_parse_quality.py:275`. Fellowship specialty and the second licence are dropped. Whole-object credential saves let an older extraction overwrite a newer upload: `save_asclepius_credentials` at `backend/team_store.py:1930`, with un-identified polls in `pollCvParse` at `landing/src/app/components/OnboardingWizard.tsx:1077`. §6 asks for a lot: attempt records, versioned candidate envelopes, structured extraction rewrite, tri-state through tiering. | All 12 anchors spot-checked resolve. **Stage it** (below) — ship the correctness fixes before the extraction rewrite. |
| **A — Applicant screen + exam blocker** (mine) | The pre-approval portal is a six-stage hidden state machine with three interstitials; and the examination's Reveal / library search / dictation 403 for every provisional applicant because the exam enters through the `TUTORIAL` door then uses full-access workspace endpoints. | See `prds/PRD_A_…` §1–§2. |

Conflicts between the three: **none** — A touches `asclepius.js` (portal) + the success screen in `steps.tsx:3860-3988`; B touches `Step5Credentials`/primitives/completeness; C touches the wizard's CV path and backend credentialing. The only shared file is `steps.tsx`, in non-overlapping regions. B's invariant "no new mandatory fields" and A's "only name/email/specialty required" agree.

---

## 1 · Execution order (five phases, one PR each)

| Phase | Source | What ships | Why this order |
|---|---|---|---|
| **0** | A §2 | `require_task_access` carve-out so a provisional applicant can reveal/search/dictate/assist on **their own exam task only**; citations search + transcribe on the `TUTORIAL` surface; exam persists like a real task | **Nobody can currently complete the examination.** Blocks every new physician's verification. Smallest change, largest unblock. |
| **1** | B P0-A, P0-B | Hoist `Group` to module scope (and any other inline component); preserve section open-state and scroll position across edits; stable keys | The typing/focus defect makes the form unusable; one structural fix resolves 18 of 21 failures. |
| **2** | A §1 | Pre-approval portal → one screen, two boxes (how to label · take the examination); interstitials deleted; success-screen copy; no Calendly pre-approval; `Any questions: tejpatel@berkeley.edu` | Depends on Phase 0 (the exam must work before the screen points at it). |
| **3** | B P1-A/B/C/D + C §6 A, C, E | Programmatic labels; row identity by stable id; blank-row completeness; honest "still missing" count · **CV lifecycle**: attempt ids, atomic terminal state, cancel obsolete polls, transactional credential merge · **tri-state board validity** (null stays unanswered everywhere: wizard, serializers, tiering `feature_vector`, admin) · fellowship specialty + second license + residency completion-year mapping · replace the test at `:275` | Correctness and data-safety; no extraction-quality work yet. |
| **4** | C §6 B, D, F | Structured extraction with evidence spans; provenance chips + clear-review UI; PDF quality/OCR bounds | Quality. Ship after 0–3 are in production and measured. |

Each phase: its own commit series citing the PRD section; the phase's tests green; the JSDOM diagnostic from `evidence/` promoted to a maintained test where the PRD says so; real-browser acceptance (typing, scroll, mobile keyboard) performed in the **sandbox realm or a test account — never by submitting a production physician application.**

---

## 2 · Design invariants across all phases

1. **No new mandatory fields.** Only name, email, specialty are required to submit (Onboarding v2). Unknown stays unanswered (`null`), never `false`, never "No".
2. **Never overwrite a user edit** — including a deliberate clear. An older extraction attempt cannot write over a newer one.
3. **No database deletion; additive migrations only.** Attempt records are a new table; credential rows are merged, never replaced wholesale.
4. **A provisional applicant reaches exactly one task: their own examination.** `_BY_ACCESS[PROVISIONAL]` is not widened. The V4 real-data wall stands.
5. **The examination persists** (independent commit + submission); the practice tutorial does not. Do not route the exam through `/tutorial/reveal`.
6. **CV content is data, not instructions.** Embedded prompts/links/QR codes never trigger actions.
7. **Design bar:** one primary per screen; tokens only; no inline component definitions inside render functions anywhere in `onboarding/` (add a lint rule); CSS outside foreign `@media` blocks.

---

## 2.5 · Tests

Each phase converts the relevant diagnostic in `evidence/` into a maintained test
and runs its own list plus the full suite. What shipped, per phase:

| Phase | Tests |
|---|---|
| 0 | `backend/tests/test_exam_task_access.py` — 24, the carve-out's edges |
| 1 | `landing/test/onboarding-form-stability.test.cjs` (promoted from `evidence/onboarding-ux-evidence/run_form_probe.cjs`) and `landing/test/onboarding-structure.test.cjs` (invariant 7 as a rule), run by the `onboarding-form` CI job |
| 2 | `backend/tests/test_applicant_home_screen.py` — 23; six existing tests that encoded the deleted design were rewritten |
| 3 | `backend/tests/test_board_validity_tri_state.py` — 15; `backend/tests/test_cv_attempt_lifecycle.py` — 20; the three Phase 1 `todo` placeholders became real assertions |

Real-browser scroll, mobile virtual keyboards, IME composition and screen-reader
output are NOT covered by any of these. JSDOM has no layout engine, so they stay
human checks in the sandbox realm (PRD B §6) and are listed as unverified in
`docs/ONBOARDING_MASTER_REPORT.md`.

## 2.6 · Do not touch / non-goals

- `_BY_ACCESS`, `require_full_access` semantics for everyone else, the
  `/tutorial/reveal` no-persistence rule, the V4 real-data wall, and the
  verification queue's read of the examination (PRD A §2.4).
- `GET /exam/task` / `POST /exam/submit` and their stage stamps, the tutorial
  internals, the post-approval first-run walkthrough, and the demo-video
  ticket/Range endpoint (PRD A §1.6).
- Tier weights, seniority or prestige features, retroactive revocation, and
  auto-approval. The tri-state work corrects the INPUT semantics of existing
  computations and changes no weight (PRD C §6-E).
- Any database deletion. The CV attempt table is additive and superseded
  attempts are retained (§2 invariant 3).
- Phase 4 (PRD C §6 B, D, F — structured extraction with evidence spans,
  provenance chips, PDF/OCR bounds). Not started, by instruction.

## 3 · The prompt for Claude Code

> Read `AGENTS.md`, then `00_MASTER_ONBOARDING_PRD.md`, then the three PRDs in `prds/` (A, B, C). Inspect the current checkout before changing anything — it contains unrelated in-flight work; preserve it. Re-verify every `file:line` citation against this checkout with `sed -n` before editing and fix the PRD text, not the code, where a citation drifted.
>
> Execute **Phase 0 through Phase 3** in order, one PR per phase, commits citing PRD sections. Do not start Phase 4. Within each phase: implement, then convert the relevant diagnostic scripts in `evidence/` into maintained tests, then run the phase's test list and the full suite. Honour the seven invariants in §2 of the master. If any step appears to require widening provisional access beyond the applicant's own exam task, deleting data, adding a mandatory field, or routing the exam through the tutorial reveal — stop and explain instead of doing it.
>
> Finish with `docs/ONBOARDING_MASTER_REPORT.md`: per phase, what shipped, what tests were promoted from evidence, what passed, and what remains unverified (real-browser scroll, mobile keyboard, IME, screen reader — these require a human in the sandbox realm; do not claim them from JSDOM).

---

## 4 · Definition of done (human-verified, in the sandbox)

A fresh provisional account: completes the wizard typing a phone number and a legal name without losing focus → uploads a CV → sees unknown board validity as *unanswered* → lands on the two-box applicant screen → watches the video → starts the examination → cites a guideline → searches the library → **reveals AI answers** → evaluates → submits → sees "Your examination is with us" → the admin verification queue shows the committed independent answer. Zero 403s, zero interstitials, zero lost keystrokes.
