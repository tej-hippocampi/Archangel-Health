# PRD C · Phase 0 — audit findings

Read of `backend/asclepius/credentialing.py` (1,032 lines), `backend/routers/onboarding.py`
(1,296 lines), `backend/routers/asclepius_verify.py` (463 lines),
`backend/asclepius/store.py` (PRD-B block, `:934–975` and `:4095–4330`),
`frontend/asclepius/onboarding.js`, `landing/src/app/components/onboarding/steps.tsx`.

Status key: **FIXED** (this branch) · **ALREADY CORRECT** (prior work; verified, test added) ·
**ASSIGNED ELSEWHERE** (outside Agent C's write allowlist) · **DESIGN CHANGE** (PRD process
revised — see `PRD_C_PROCESS_REVIEW.md`).

---

## §1.1 The NPI tri-state

**ALREADY CORRECT.** Every return path in `fetch_npi_record` was traced:

| Path | Returns | Correct? |
|---|---|---|
| `httpx` raises (timeout/DNS/TLS/reset) | `unavailable` | ✅ |
| HTTP 429 | `unavailable:rate_limited` | ✅ |
| any non-200 | `unavailable:http_N` | ✅ |
| 200, body not JSON | `unavailable:bad_json` | ✅ |
| 200, `Errors`/`errors` present | `unavailable:api_error` | ✅ |
| **200, `result_count` key absent** | `unavailable:unrecognized_payload` | ✅ — this is the one that matters |
| 200, `result_count == 0` | `not_found` | ✅ |
| 200, `result_count > 0` but `results` malformed | `unavailable:unrecognized_payload` | ✅ |
| bad Luhn checksum | `not_found`, **no network call** | ✅ |

The permanence argument holds downstream: `store.set_npi_result` (`store.py:4105`) stamps
`npi_checked_at` **only** on `verified`/`mismatch`/`not_found`. An `unavailable` writes to
`npi_last_attempt_json` and returns early, so it never satisfies the 30-day cache
(`get_cached_npi_fetch` requires `npi_checked_at IS NOT NULL`) and never suppresses the retry
sweep (`users_pending_npi_recheck` selects on `npi_checked_at IS NULL`).

Regression tests added in `test_credentialing_audit.py` covering all nine paths plus the
"unrecognized 200 does not become a 30-day cached rejection" property.

## §1.2 Name matching

**Two of three ALREADY CORRECT; one real bug FIXED.**

- `_normalize_family_name` NFKD-folds and drops combining marks (`credentialing.py:113`), then
  hand-maps `ß ø đ ł`, which have no combining-mark decomposition. `Muñoz` → `munoz`,
  `García` → `garcia`. **Correct.**
- `family_name_from_legal_name` (`credentialing.py:144`) drops a trailing comma-clause, strips
  honorifics, and skips suffix tokens with an arity rule so `Minh Do` keeps `Do` while
  `Jane Doe DO` keeps `Doe`. **Correct.**
- **BUG — FIXED.** `routers/asclepius_verify.py:54` `_family_name()` used
  `legal.split()[-1]`, the exact defect the PRD names. Every admin **Recheck NPI** click and
  every bulk recheck sweep for a physician who typed `"Jane Doe, MD"` compared `"MD"` against
  the registry and wrote a `family_name_mismatch` — turning a verified physician into a
  blocker-flagged row *on the button an admin presses to help them*. The signup path already
  used `family_name_from_legal_name`, so the two paths disagreed about the same physician.
  Now both call the same helper.

## §1.3 Signup blocking

**ALREADY CORRECT.** Both `/asclepius/finish` (`onboarding.py:1148`) and `/member/finish`
(`onboarding.py:1265`) reach `_provision_asclepius_user` through `run_in_threadpool`, so the
synchronous `httpx` → NPPES call, the pbkdf2 hash and the sqlite writes are all off the event
loop. `_timeout_for()` bounds the worst case to ~9 s wall-clock per phase rather than 6 s × 4.
A path test with NPPES stubbed to hang for 30 s now asserts the response returns in < 500 ms
(DoD §7.7).

## §1.4 Rate limiting

**ALREADY CORRECT.** `_signup_rate_guard` (`onboarding.py:86`) keys on a SHA-256 of the
onboarding **token** first (6/hour — one account per token by construction), with a much looser
per-IP ceiling (20/hour) and a global volumetric backstop (300/hour). The NAT case named in the
PRD is handled: ten physicians behind one hospital gateway each carry their own token.

## §1.5 Field capture

**ALREADY CORRECT — receiver *and* sender both exist.**

| Field | Sender | Receiver |
|---|---|---|
| `phone` | `steps.tsx:1534` (physician's own mobile, required, ≥7 chars) → `credentials.phone` | `onboarding.py:768` → `store.update_identity_capture` |
| `linkedin_url` | `steps.tsx:1547` → `credentials.linkedinUrl` | `onboarding.py:769` |
| `cv_asset_sha` | **never client-sent by design** — written server-side at upload (`onboarding.py:1007 _record_cv_on_person`), and `_preserve_server_cv_fields` strips any client-supplied sha | `onboarding.py:784` → `store.set_cv` |

The `cv_asset_sha` arrangement is deliberately *not* a sender/receiver pair: a client-named sha
would be an unvalidated reference into the same asset store that holds de-identified clinical
images.

---

## Findings the audit surfaced that the PRD did not ask for

### C-0.6 — `basic.sex` was never stripped *explicitly* (FIXED)

`_trim_record` is an allow-list, so `basic.sex` did not survive into `npi_payload_json` by
accident of construction. That is not the same as a boundary. Nothing asserted it, nothing
tested it, and the raw NPPES record — which *does* carry `sex` — is passed whole to
`_registry_family_names` and `_basic`. One future edit that widens the trim to
`dict(record["basic"])` reintroduces a protected attribute into the database silently.

Added `_strip_protected_attributes()`, applied at the ingest boundary in `fetch_npi_record`
before the record is returned to any caller, plus `assert_no_protected_attributes()` used in
tests. `sex`, `gender`, `date_of_birth`, `dob`, `birth_date` and `name_prefix` are dropped from
`basic` and from the record root. This is §2's "live adverse-impact hazard sitting in an API
response", closed at the one place every path goes through.

### C-0.7 — the signup form collects medical school and graduation year (ASSIGNED / MITIGATED)

`landing/src/app/components/onboarding/steps.tsx` collects `medicalSchool: {institution, year}`
and `residency[].year`. PRD C §3.3 says: *never collect, derive or log* medical school name or
rank, and graduation year. These land in `users.credentials_json` verbatim.

Agent C owns `steps.tsx`, so the form is in scope — but removing residency **year** breaks gate
A4 (*"Residency complete, not in training — attestation + year"*), which the same PRD requires.
The two requirements are in direct conflict.

Resolution taken, in ascending order of cost to the physician:

1. **A hard boundary, not a promise.** `tiering.FORBIDDEN_CREDENTIAL_KEYS` and
   `tiering.feature_vector()` refuse to read any of these keys. `test_tiering_score.py`
   asserts that mutating `medicalSchool` / `medicalSchoolYear` / `gradYear` / `dob` /
   `sex` / `practiceZip` across the full plausible range changes the score by **exactly 0.0**.
   That is a property test over the encoder, not a code-review convention.
2. **Residency year is consumed once and discarded.** It is reduced to the single
   boolean `post_residency_ge_3yr` at the encoder boundary; the continuous value never reaches
   a feature. This is exactly §0.2's "capped binary gate, never a continuous scaling term".
3. **Medical school institution is removed from the form.** It is not needed by any gate.
   Graduation *year* of medical school is likewise removed. Residency institution + completion
   year are kept, because A4 has no other evidence source.

Left for a human: counsel review per context pack §7.4 (NYC Local Law 144). Storing residency
completion year is still an age proxy in the database even though it cannot reach the model.
The mitigation is that it is one boolean away from the score and the boolean is auditable.

### C-0.8 — `propose_tier` lets a score outrank a missing NPI (FIXED by Phase 1)

`credentialing.propose_tier` treats `npi_result == not_found` as a `±0` scoring note. A
physician with an academic email, a board certification and 10 years' experience scores
10+20+20 = 50 ≥ `LABELER_MIN_SCORE`, so **an unfindable NPI still proposes `labeler`**. PRD §2
makes A1 a hard gate: failing any gate is eligible for neither tier.

The legacy scorer is left in place and untouched (the admin queue's existing column depends on
it, and §8 forbids backfilling). The new `tiering.propose()` is gate-first: `hard_gates()`
returns `blocked` before any score is computed, and `test_tiering_score.py` asserts a maximal
score cannot open a closed gate (DoD §7.2).

### C-0.9 — `_duplicate_npi` runs an unbounded query per dossier open (ACCEPTED, not fixed)

`asclepius_verify.py:57` runs `SELECT * FROM users WHERE <normalized npi> = ?`, unindexed
because the comparison is a five-deep `REPLACE()` over the column. The queue path already
avoids this via `npi_claim_counts()` (one grouped query), so the residual cost is one full scan
per *dossier expand*, at admin click rate. Not worth an index on a computed expression at this
volume. Recorded so the next person does not rediscover it as a mystery.

### C-0.10 — sentinel-name collision in `store.py` (RESOLVED by renaming *this* block)

The context pack tells Agent C to use a `PRD-C sentinel` block in `store.py`. `store.py` already
has one, at `:1033` and `:4808` — the **health-system** schema from an earlier release, owned by
a different agent. Reusing the label would make the expected four-way merge conflict
unresolvable by inspection.

This release's block is therefore labelled `PRD-CRED TIERING` and inserted at the same two
insertion points, immediately after `END PRD-D`. No existing block is touched.

---

### C-0.11 — the calibration exam had no retake policy (FIXED)

Not a Phase 0 item; found auditing my own Phase 3 work before commit. `GET /calibration/exam`
minted a fresh attempt on every call, and `latest_calibration_for_user` reads the most recent
submitted one. So a candidate could:

1. refresh until an easy sample of items came up (the pool is larger than the exam), and
2. re-sit indefinitely until one attempt cleared 0.85 — while each attempt leaked the key by
   trial and error, since every attempt draws from the same pool and reveals which answers
   scored.

Either one makes the exam not a gate, and the exam is the only feature in the model with a
clean legal footing. Now: an unsubmitted attempt is **resumed**, never replaced; two submitted
attempts per specialty; a 90-day cooldown after that; and passing closes the door on a retake,
because re-sitting after a pass can only lower it. Attempts are counted **submitted**, not
started — a browser closed mid-case is not a data point about the physician.

### C-0.12 — I re-created the banned expert-acceptance definition (FIXED before commit)

`store.paired_label_observations` initially selected the TR's near-gold label with
`cr.verdict IN ('accept', 'accept_with_edits')`. Two Seam-3 guard tests caught it
(`test_health_systems.py::test_expert_acceptance_has_exactly_one_definition`,
`test_review_tier.py::test_only_one_definition_of_expert_acceptance_exists`) — this is the exact
rival number that once had the admin dashboard reading ~97% while `quality_report.md` read ~84%.

The fix is not to smuggle it past the grep. What Dawid–Skene needs is weaker and genuinely
different: a TR who did **not reject** a submission has endorsed its chosen answer well enough
to anchor the EM. That is `verdict != 'reject'`, named "not rejected" in the code, and it is
never called acceptance. `agreement.review_acceptance` remains the single definition.

### C-0.13 — a guard function that was a no-op with a docstring claiming it raised (FIXED)

My first draft of `tiering._assert_no_forbidden_reads` did nothing and returned `None`, under a
name and a comment asserting that `feature_vector()` raises on a protected proxy. That is worse
than no guard: a reviewer reads the name, believes the property is enforced, and stops looking.

Replaced with a real import-time check. The exact set of credential keys the encoder reads is
declared as `ENCODER_CREDENTIAL_KEYS`, and the module refuses to import if it intersects
`FORBIDDEN_CREDENTIAL_KEYS`.

---

## Phase 0 gate

Every item in PRD C §1 is fixed, verified-already-correct with a new regression test, or
explicitly assigned above. Run:

```
python3 -m pytest backend/tests/test_credentialing_audit.py -q
```

Full Definition of Done (§7), plus the admin-surface DOM tests:

```
python3 -m pytest backend/tests/test_tiering_score.py backend/tests/test_tiering_learning.py \
                 backend/tests/test_credentialing_audit.py backend/tests/test_tiering_admin_dom.py -q
```

Departures from the PRD's specified process — including two defects in the specified learning
update that fail in exactly the shape §8 warns about — are documented in
`PRD_C_PROCESS_REVIEW.md`.
