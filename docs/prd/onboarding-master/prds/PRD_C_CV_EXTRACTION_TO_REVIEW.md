# PRD: Reliable physician CV extraction into onboarding review

Status: implementation-ready specification, not an implemented fix.
Date: 7 September 2026 (America/Los_Angeles).
Owner: Archangel Health / physician onboarding.

## 1. Outcome and scope

A physician uploads a CV and reaches a review page containing supported, correctly associated facts. They correct or confirm those facts once. Missing information stays visibly unanswered. Uploading a replacement, refreshing, or editing while extraction runs must not lose their work or apply a different document's result.

Implement this for the Asclepius physician onboarding CV-to-review path, including the invited-member path where it shares extraction and credential components. Preserve manual entry and the current minimal submission requirements. This is extraction and review assistance, not automatic credential verification or a new physician-ranking model.

The supplied Mike Blum PDF is a useful baseline, but it was deliberately formatted to fit the current parser. Passing that sample is insufficient. The accompanying corpus includes negative statements, mixed PDF content, international qualifications, and lifecycle failures.

## 2. Evidence: input, live server output, visible form

Inspected the existing Google Chrome onboarding review page without editing or submitting it. Also read the same upload's live CV-status endpoint through Chrome. Its response reported filename Mock_Physician_CV_Mike_Blum.pdf, stage done, finished true, ok true, 1,522 extracted characters, and parsed_at 2026-09-08T02:44:46 UTC. No onboarding tokens or token-bearing URLs are included in this handoff.

The live business fields matched the local parser output: Mike Blum; MD and MPH; Nephrology; 10 years; ABIM Nephrology and ABIM Internal Medicine; residency at Harbor Example University Hospital ending 2014; fellowship at Cedar Example Medical Center ending 2016; CA TEST-900001 and MA TEST-900002; Cedar Example Medical Center as employer; and the synthetic NPI and LinkedIn URL. The two board-validity values were null in the server response.

### What worked in the visible form

- NPI, MD, Nephrology, 10 years, employer, LinkedIn, board issuer/field, training institution/year, and the first license were populated consistently with the document.
- Mike Blum appeared as the legal name, without a CV chip. The local wizard seeds the signup name, so the observation does not establish that the CV overwrote or supplied that name.
- The mobile field was empty. Its gray +1 (555) 010-7788 was a placeholder, not an extracted phone number. The CV labels its number only “Phone,” not “Mobile”; do not silently promote a generic number to a personal mobile.

### Confirmed problems and deliberate omissions

1. **Incorrect negative assertion:** both board-validity controls had No selected. The backend's null became false in the frontend. An unknown answer is not a negative answer. Manual new board rows also default to true in local source, creating the opposite unconfirmed assertion.
2. **Fellowship specialty lost:** the document explicitly says Fellow, Nephrology; only institution and year reach the form. The apparent Nephrology text in the empty fellowship field is a placeholder.
3. **Residency year duplicated but not propagated:** the residency row shows 2014; the separate completion-year field is empty (2010 is its placeholder).
4. **Second license hidden:** both licenses are extracted, but only CA is shown in the single-license form. The physician cannot review the MA fact there.
5. **Missing routing context:** CKD, dialysis, hypertension, and the current employer's Los Angeles location are present in the input but absent from the corresponding form fields. Extraction must keep clinical focus separate from board-certified subspecialty.
6. **Misleading completeness scope:** the live page says “1 still missing” while several confirmation questions remain unanswered. That may mean a selected subset of useful fields, but it reads as an inventory of every missing answer. Clarify the count; do not make optional fields newly mandatory.

Intentional omissions: current activity, board validity, MOC/CC participation, clinical workload, language proficiency, and signed attestations cannot be inferred merely from a job title or dates. Medical-school prestige, age proxies, or a publication count must not become new review-score inputs. The NPI is a checksum-only synthetic fixture, not a verified registry identity.

Evidence limits: deployed build identity was not established. Local HEAD and relevant file hashes are recorded in cv-extraction-evidence/source_manifest.json. The local checkout changed during this investigation; the later UX snapshot includes the summary/accordion implementation and the public compiled bundle confirms its nested-wrapper pattern. Treat each evidence manifest as its own snapshot and re-check the implementation checkout. Production race conditions, OCR availability, and refresh behavior were not induced or measured.

## 3. Reproduced diagnostic results

These are targeted checks against current code and proposed acceptance behavior, not population accuracy estimates or a full passing test suite.

- All 9 existing CV fixtures met their declared expected values in a local field-by-field replay.
- Of 27 additional text probes, 10 met proposed expectations and 17 exposed defects or absent capabilities.
- Of 6 generated PDF probes, 4 met their selected assertions. Mixed text/raster and image-only PDFs did not extract the name/NPI. OCR is unavailable in this local environment; the image-only result is an environment limitation, not proof that deployed OCR fails. The mixed-content defect also follows from the document-wide text-length heuristic, which skips OCR after finding enough unrelated text.
- Of 10 executable frontend-helper checks, 3 met proposed expectations and 7 exposed gaps. The actual TypeScript functions were executed after stripping types, not reimplemented. React rendering was checked separately in Chrome for the existing upload.
- An unchanged worker recording function, executed with an in-memory store, allowed older upload A to overwrite upload B's asset and parse while retaining filename B.pdf. This was a local interleaving, not a live incident.

Named failures to fix:

- Accented names and “Dr. Mike Blum” are missed; a name below eight header lines is missed.
- A reference author's MD becomes the applicant's degree.
- “Not board certified in Nephrology” and a pending board examination become certifications.
- “Not active” becomes an active license.
- A license written “License: A123456” or split over adjacent lines is missed.
- A single-line fellowship puts role, institution, and dates together into institution.
- “present” in lowercase loses a current employer; “Selected Publications 2020 - Present” becomes an employer.
- A stated career break is ignored when years since training are labeled years in active practice.
- A two-line board/issuer layout invents an Internal Medicine certification from the issuer name and loses the issuer association for Nephrology.
- Explicit mobile, languages, primary international qualification, and non-US registry details are absent capabilities.
- A preexisting NY license state gets paired with the parsed CA license number because fields merge independently.
- Re-upload preserves an old auto-filled employer as if it were user-authored; extra certifications silently truncate.

## 4. Code map and root causes

Each anchor below refers to the inspected local tree. Re-run the citation audit against the implementation checkout before editing.

- `applyCvParse` — landing/src/app/components/OnboardingWizard.tsx:336. Scalar empty-only fill, array-wide untouched guards, unknown-to-false board mapping, first-license selection, and omitted fellowship subject/completion-year mapping.
- `emptyCredentials` — landing/src/app/components/onboarding/steps.tsx:228. Manual board defaults and country defaults.
- `BoardCert` — landing/src/app/components/onboarding/steps.tsx:82. Board active is currently boolean only.
- `YesNoToggle` — landing/src/app/components/onboarding/primitives.tsx:886. It already supports null; false visibly selects No.
- `loadDirectorSession` — landing/src/app/components/OnboardingWizard.tsx:488. Saved CV metadata counts as saved credentials; parse and fields hydrate separately, chips clear, and uploaded sessions route to review without reapplying a completed parse or resuming an in-progress poll.
- `pollCvParse` — landing/src/app/components/OnboardingWizard.tsx:987. Polling is initiated by upload, has no upload-attempt identity, and stops at terminal stage with whatever parsed payload arrives.
- `asclepius_cv_upload` — backend/routers/onboarding.py:1631. Upload records metadata and schedules a background task.
- `asclepius_cv_status` — backend/routers/onboarding.py:1692. Returns stage and parsed state from the mutable credential blob.
- `_record_cv_on_person` — backend/routers/onboarding.py:1754. Read/modify/write of the whole credential object, without attempt identity or atomic field merge.
- `_parse_cv_into_person` — backend/routers/onboarding.py:1772. Every stage and final parse can write after a newer upload. A terminal callback can precede the final result write; a failure can leave an old parsed payload.
- `save_asclepius_credentials` — backend/team_store.py:1893. Replaces the complete credential JSON; a SHA-only check does not prevent a worker from losing a simultaneous user edit.
- `_pdf_text` — backend/asclepius/credentialing.py:858. OCR is selected using total document text length, not page-level completeness.
- `_ocr_pdf_pages` — backend/asclepius/credentialing.py:880. First-five-page OCR cap without an explicit partial-document result contract.
- `_extract_name` — backend/asclepius/credentialing.py:994. First-eight-line and ASCII-oriented name assumptions.
- `_extract_degrees` — backend/asclepius/credentialing.py:1013. Whole-document degree scanning without applicant/section association.
- `_extract_board_certifications` — backend/asclepius/credentialing.py:1233. Recognition/association needs negation, pending/expiry, and multiline issuer handling.
- `_extract_licenses` — backend/asclepius/credentialing.py:1356. Same-line formatting restrictions and positive-token matching without negation.
- `_extract_training` — backend/asclepius/credentialing.py:1443. No explicit subject in result; single-line institution includes the full input line.
- `_extract_employer` — backend/asclepius/credentialing.py:1510. Date header is insufficient evidence of employment.
- `_parse_cv_text` — backend/asclepius/credentialing.py:1533. Limited schema; elapsed training years are used as active practice; section/person relationships are lost.
- `parse_cv` — backend/asclepius/credentialing.py:1651. A long readable document may report ok even with no useful credentials; terminal notification occurs before the caller persists the result.

Downstream consumers are part of the tri-state fix, not optional cleanup:

- `feature_vector` — backend/asclepius/tiering.py:663. Inspect active-null handling and the legacy board-text fallback.
- `domain_match` — backend/asclepius/tiering.py:496. Subspecialty evidence also depends on active-state semantics.
- `_provision_asclepius_user` — backend/routers/onboarding.py:1153. Inspect legacy board-text projection from credentials.
- `propose_tier` — backend/asclepius/credentialing.py:1769. Inspect legacy scoring from board text/CV lists.
- `test_a_certification_never_arrives_pre_ticked_as_active` — backend/tests/test_cv_parse_quality.py:275. The current source-string test expressly expects false and therefore protects the observed bug. Replace with behavior tests for unanswered/Yes/No.

## 5. Design invariants

1. A document claim, a parsed suggestion, a physician's confirmation, and registry verification are separate states. Never turn one into another implicitly.
2. Every auto-filled value has a source span and a document/attempt identity. Prefer an empty field to an unsupported or conflicting value.
3. Never overwrite a user edit, including intentionally clearing a field, or independently merge members of a coupled credential.
4. Unknown remains null/unanswered throughout UI, persistence, serializers, eligibility, and admin display. Null must not select No or earn active-certification credit.
5. No new mandatory fields, silent application submission, automatic approval, altered tier weights, or database deletion. Existing reviewed decisions remain intact.
6. No one-document-per-applicant assumption: support replacement, multiple training entries, licenses, and certifications without silently discarding supported facts.
7. Archived CVs remain evidence, not executable instructions. Embedded prompts, links, scripts, and QR codes must not trigger actions or external requests.

## 6. Required implementation

### A. Atomic upload/result lifecycle — first release

Introduce server-owned extraction attempts in a separate additive record, scoped to realm + organization + person, with an immutable attempt ID, asset SHA, filename, parser version, timestamps, state, and result. Keep a pointer to the person's current attempt. SHA alone is insufficient because the same file can be uploaded twice.

Suggested states: queued, reading, extracting, ready, partial, failed, superseded. Provide a legacy response adapter for current clients and their reading/matching/preparing/done/failed stages; do not break older clients during rollout.

- Beginning B atomically makes B current. Retain A as superseded history; only B may populate current suggestions. A's late stage, success, or failure must not alter B's state/result/filename.
- Commit terminal state and the corresponding result/failure together. Internal progress callbacks must never publish done before the new result is durable. A failed B must not return A's ok=true result under B's identity.
- Separate extraction progress from confirmed credentials. Use transactional merge plus credential revision checking for user edits; whole-object worker saves are forbidden. Concurrent unrelated credential edits survive.
- Persist jobs so a process restart cannot strand them forever; recover pending work or mark it failed with a retry path. Keep bounded retries and idempotent completion.
- Every upload/status response carries attempt ID and parser version. The client cancels obsolete polls on re-upload/unmount, ignores responses for older attempts, and resumes the current pending attempt after refresh.
- On resume after extraction finished but before credentials were saved, merge current suggestions with explicit provenance. Do not treat the existence of server CV metadata as proof that review fields were saved.
- A timeout or failed extraction lands on manual review with an honest retry message; it does not clear typed answers. No indefinite spinner. Retain the existing ability to proceed manually.

### B. Structured extraction with evidence

Preserve the existing parsed keys through a compatibility adapter. Add a versioned result envelope containing document quality, pages processed/total, warnings, and field candidates. Candidates include normalized value, original text, page/region or line span, rule/method, and status: supported, ambiguous, missing, or rejected. Rejected here means excluded from prefill, not rejection of the physician.

Do not invent numeric confidence thresholds without calibration. Initial auto-fill policy is deterministic: fill only supported, non-conflicting candidates belonging to this applicant; offer ambiguous candidates for selection. Missing/rejected data stays blank with a concise explanation when useful.

Build section and entry boundaries before field matching. Keep role, institution, dates, specialty, and issuer associated across line breaks and columns. Support Unicode names, honorific stripping, punctuation, uppercase headings, and common date variants. Do not convert all text to ASCII or scan references/coauthors as applicant identity. Canonicalize degrees without assuming that an Indian postgraduate MD is the primary qualification when MBBS is explicitly labeled primary.

A semantic model is not required for the first implementation. Do not solve these deterministic defects by sending entire CVs to an unconfigured provider. If an existing approved model path is used later, require the same schema, source evidence, negative-case tests, timeout/fallback, and separate quality evaluation; document content remains untrusted data.

### C. Field contract and review behavior

**Identity and contacts**

- Legal name: recognize applicant header/name labels; preserve diacritics and meaningful punctuation. Preserve signup-entered name. If CV conflicts with it, show a suggested correction instead of silently replacing it.
- Primary degree/qualification: capture explicit primary qualification and retain additional degrees as evidence. Do not make MPH the primary medical degree. Do not infer applicant degrees from references or publications.
- Primary specialty: explicit profile/appointment specialty outranks board-list order. Distinguish a clinical focus, a trained specialty, and a certified subspecialty; do not manufacture a second certification from a board issuer's title.
- NPI: preserve the existing labeled/checksum test, retain association to applicant, and flag competing labeled candidates for selection. A valid checksum is not a successful registry lookup. Never call the synthetic NPI verified.
- Phone: automatically suggest a clearly labeled applicant mobile/cell number. A generic “Phone” or office number remains a candidate requiring confirmation of personal-mobile use; fax, references' phones, and unlabeled number runs do not prefill mobile.
- Login email: preserve OTP-verified account email. A CV contact email may be shown as reference but cannot change authentication identity.
- LinkedIn: capture only an applicant-associated profile URL, not a coauthor/reference link. Never navigate it as part of parsing.

**Training and certifications**

- Training entries carry stable IDs, kind, institution, specialty, start/end dates, and completion-status evidence. Parse same-line and adjacent-line forms to the same structure. Institution excludes role, dates, and city.
- Fill fellowship specialty when explicitly attached to that fellowship. Do not guess it from the physician's current primary specialty.
- For one unambiguous completed residency, suggest its end year in the existing completion-year field as well as the row. A current/future residency uses an expected end year, not a completed year. Multiple residencies require a primary/relevant selection; do not substitute fellowship year. The “Have you finished residency?” attestation remains unanswered until the physician confirms.
- Board validity becomes boolean-or-null. New manual rows and imported rows default to null. Yes and No represent the physician's answer and are selected only by explicit user action. Store registry-verified status separately; never make it appear that the physician supplied an answer they did not give. Capture document wording such as active/expired/pending separately; do not pre-answer a current-status attestation from it.
- Negative, pending, expired, and historical certification mentions remain evidence with their status; never display them as a currently-held certification without qualification. “Not board certified” produces no held-certification suggestion.
- Preserve all extracted entries within documented limits. If more entries are found than the UI initially displays, show “More credentials found” and expand/review them; do not silently slice arrays.

**Licensing and geography**

- Introduce repeatable licenses with country, jurisdiction/state, number, issuer/registry, document status, and evidence. Support punctuation and adjacent-line layouts; recognize negation before positive tokens.
- Country/state/number/registry extras are one coupled value. Never form NY + a CA number. Existing user state with a conflicting CV license creates a choice, not a partial automatic patch.
- Maintain the existing single-license fields as an atomic projection of an explicitly selected primary license. For a fresh single-country form, suggest the sole compatible license; with multiple compatible candidates require a primary selection. Preserve all others for review/admin evidence.
- Separate country of practice, licensure, and qualification. Suggest them only from explicit relevant address/registration evidence; never infer country from a person's name or medical-school reputation. Unknown geography remains unanswered, not a claimed US fact. Plan compatibility with the existing US-default presentation separately from confirmed data.
- For non-US CVs support an explicitly labeled registry identifier and the selected country's configuration. Do not force it into NPI or a US state field. Where no registry format is modeled, retain raw text for manual confirmation.

**Practice and routing context**

- Employer and practice city come from the current clinical appointment, respecting chronology, employment section, and address association. Publications, training-only entries, past jobs, and future offers cannot become current employer.
- Auto-suggest explicit clinical focus areas such as dialysis, CKD, hypertension. Keep the source wording and canonical alias; do not promote interest into certified subspecialty.
- Languages, practice settings, and structured review experience may be suggested only from explicit applicant statements. Document language is not spoken-language proficiency; job title alone does not establish workload or MOC participation.
- Years in active practice requires an explicit supported statement. Elapsed time since training is a different fact; do not convert it into active years, particularly through career breaks or leave.
- Current activity, leave status, workload, MOC/CC, board validity, legal consents, signatures, and disciplinary attestations remain explicit confirmations. CV hints may be shown beside them, with neither Yes nor No preselected.

### D. Provenance, editing, and clear review UI

Track field/row provenance separately from values: source attempt, source span, last suggested value, user-edited/user-cleared state, and confirmation state. Clearing a suggestion counts as an edit and survives refresh/re-upload.

- A replacement may update a previously auto-filled value only if it is still unedited/unconfirmed and its provenance points to the superseded attempt. Otherwise show a proposed change. Do not remove an old field merely because the replacement CV omits it; mark its old-source status for review.
- Merge repeatable rows by stable identity, never array position; preserve partially edited rows and unrelated siblings. Do not block all new extracted rows because one old row was edited.
- Use leaf-level “From your CV” indicators; a chip on the whole fellowship or certification must not imply its empty/confirmation fields came from the document.
- Persist provenance across reloads. Reloading does not mean the physician reviewed an answer. Distinguish “Suggested from CV” from “Confirmed by you.”
- Replace “1 still missing” with scoped language such as “You can submit now. 10 details suggested; review the highlighted details. Mobile number can help verification.” Optional confirmation counts must be labeled and accurate; no implication that every unanswered field is required.
- Use clearly example-prefixed placeholders in empty specialist fields; gray “Nephrology” currently looks like a populated value. Accessible labels and pressed states must expose the same truth as the visual form.

### E. Nullable board state across all consumers

Changing the UI type alone is unsafe. Current downstream code can treat anything other than explicit false as active, and legacy board text can override a corrected structured state.

Audit every board consumer, including legacy projection/scoring, domain matching, reviewer eligibility, admin displays, exports of credential data, and both onboarding modes. Unknown must remain unconfirmed and contribute no affirmative active-certification signal. Legacy certification-claim credit is distinct from current-validity credit: retain the existing claim signal policy unless an explicitly documented, separately tested policy change is approved. This PRD does not remove all historical claim credit or redesign legacy ranking. Do not simply replace a single predicate while leaving a legacy text OR fallback that resurrects active status. Separate “has a certification claim” from “confirmed currently active.”

Migration is additive and source-aware. Preserve existing reviewed approvals and explicit human answers. Do not rewrite every old false to null or treat every old true default as confirmed. Retain legacy values and tag ambiguous provenance as unconfirmed for future interpretation, with human review where needed. Any compatibility fallback must require recorded confirmation/verification provenance; it must not infer it from nonempty board text. Explicit negative current-status answers remain negative.

This change corrects the input semantics of existing eligibility/feature computations. It must not change tier weights, add seniority/prestige features, retroactively revoke access, or auto-approve anyone.

### F. PDF quality and operational bounds

- Extract text and layout per page. OCR image-only pages or meaningful image regions even when other pages/header text already exceed 40 characters. Merge OCR/text spans without duplicates and preserve reading order.
- Test normal text PDFs, two columns, tables, wrapped entries, scanned pages, mixed scan/text, rotations, and repeating headers. Mark processing as partial when pages/regions cannot be read; do not label an unrelated long text layer a successful complete CV extraction.
- Detect encrypted, corrupt, blank, and unsupported documents with actionable messages and manual fallback. A readable document with zero supported applicant fields returns partial/no useful fields, not an unqualified success promise.
- Retain the current 10 MB upload limit and byte-level MIME validation. Proposed initial processing bounds: at most 30 pages, 60 seconds per attempt, and bounded OCR concurrency; expose any skipped pages as partial and offer a shorter upload/manual entry. These are implementation targets to validate, not measured production capacity. Enforce resource limits where computation actually runs, including OCR subprocesses and decompressed Word/XML size, rather than only on the HTTP request timer.
- If OCR is unavailable, say “We could not read the scanned pages; upload a text PDF or enter these details.” Surface a deployment health check, not an endless silent empty parse. Text-only uploads should still work.
- Preserve private CV access, MIME validation, metadata handling, realm boundaries, and authorized-owner checks. Log stage, timings, parser version, quality counters, and correlation IDs, not raw CV text, names, identifiers, tokens, or full URLs.

## 7. Tests and acceptance criteria

Turn the supplied diagnostics into normal regression tests. Some probe expectations describe new capabilities and are intentionally failing on current code. Probe exit zero means the experiment completed, not that all proposed acceptance criteria passed. Do not weaken negative expectations to make the old parser pass.

### Baseline end-to-end acceptance

Upload the supplied Mike Blum PDF into an isolated test signup and assert the actual review controls, saved payload, refreshed session, and admin evidence:

- Correct primary facts stay correct; fellowship specialty is Nephrology; the residency completion-year suggestion is 2014.
- CA and MA licenses are both reviewable; the chosen primary license is a coherent tuple.
- Board validity remains unanswered, not No; neither unknown nor legacy bare text earns active-certification credit. Deliberate Yes/No survives save and reload.
- CKD, dialysis, hypertension, and Los Angeles are source-supported suggestions. The generic phone is offered for confirmation rather than asserted to be a mobile.
- Existing user edits and OTP identity remain intact. Nothing is submitted or approved by extraction.

### Corpus and negative cases

Require zero unsupported affirmative credential values on the named negative corpus and exact expected field values on the supported baseline fixtures. Measure field-level precision/coverage by category; do not report one aggregate score that hides name/license mistakes behind easy fields.

Extend the 27 probes with: hyphenated/apostrophe/multipart names; multiple MDs in reference sections; credentials in publication titles; board-eligible versus board-certified; expired/renewal-pending credentials; several specialties and dual appointments; two current employers; same license number in different jurisdictions; training interrupted by leave; expected versus completed dates; MD/MBBS/MBChB/DO/PhD combinations; labeled mobile versus fax/office; several labeled NPIs with applicant association; and URL punctuation/line wrapping.

Run real input files through the byte reader, not just text strings: the six supplied PDFs plus rotated/low-resolution scans, scan on page six, mixed-page documents, password-protected/corrupt PDFs, long CVs exceeding bounds, Word tables, RTF, JPEG/PNG, and decompression/resource-limit tests. OCR-required CI jobs must have a working engine and assert actual fields. Separate explicit OCR-unavailable tests must assert the useful fallback, not pretend extraction succeeded.

### Lifecycle and integration tests

1. A slow upload A finishes after B: only B is current; filename, asset, result, and stage remain consistent.
2. The same PDF is uploaded twice: distinct attempts prevent stale completion.
3. Terminal polling between callbacks and final persistence never returns done with missing/old data.
4. B fails after A succeeded: no stale A result is applied as B.
5. A user edits/clears a field while parsing; a credentials save races a progress update; edits survive both interleavings.
6. Refresh during parsing, after parsing before first save, after partial failure, and after user corrections restores the proper state and resumes only the current job.
7. Replace a CV after unedited suggestions and after manual edits; former values can be proposed for update, latter are preserved.
8. Existing NY state plus CA number cannot form a hybrid license; repeatable rows merge without duplicates or silent loss.
9. Untouched manual board rows, imported null, explicit false, explicit true, ambiguous legacy defaults, legacy board text, and prior reviewed decisions exercise every consumer and both onboarding modes.
10. Country/qualification switching, missing fields, partial arrays, multiple licenses, empty/failed parsing, older-client responses, and inaccessible/superseded assets retain compatible behavior.

Use the existing CV fixture/tests as the starting point, add actual frontend behavior tests rather than source-string-only assertions, and register every new backend test file in the existing CI sharding system. No tests may silently fall out of CI.

### Performance and release acceptance

Measure upload-to-review separately from extraction time. Proposed staging targets on representative files: text-PDF p95 under 5 seconds; ordinary scanned CV p95 under 30 seconds; terminal recovery/fallback within 90 seconds including the client. Record file/page distributions, OCR environment, and achieved timings. Targets are not current benchmarks. If bounds are hit, visible partial/failure recovery is mandatory.

## 8. Implementation order and deliverables

1. Establish failing behavior tests for null/false, coherent license merge, terminal persistence, stale attempts, and resume. Implement lifecycle/provenance storage and coordinated tri-state consumer changes first.
2. Build section/entry-aware extraction and evidence schema; fix negation, ownership, dates, and associations. Preserve the existing successful corpus.
3. Implement field mapping, repeatable licenses, source-aware merge, international qualification handling, and clearly scoped review UI.
4. Add per-page PDF/OCR quality handling, bounded processing, and observable fallback. Run real-file and frontend integration tests.
5. Run the repository's affected checks, PRD citation audit, and independent audit; resolve findings before opening a PR. Include migration/backward-compatibility evidence and production-rollout limits.

Deliver production changes, additive migrations, fixtures, automated tests, short operator documentation, measured quality/timing report, and a concise PR summary explaining before/after behavior. Use an isolated branch/worktree and preserve unrelated in-progress bulk-media changes.

## 9. Do not touch / non-goals

Do not rewrite unrelated application auth, patient/peri-op functionality, billing, clinical-case generation, bulk-media work, or the physician-ranking model. Do not change signed/legal attestations, submit this live test application, delete database rows, auto-approve doctors, or use real physicians' identities as fixtures. Do not add medical-school prestige, graduation year, or protected proxies as score inputs. Do not upload the context pack or real CVs to external model services as part of this task.

Follow repository AGENTS.md and existing realm/store patterns. If implementation touches the protected data categories named there, run data-inventory before and after. This investigation changed only documentation/evidence; it did not modify production code or live applicant fields.

## 10. Handoff evidence and audit

The adjacent cv-extraction-evidence folder contains the baseline PDF/TXT, expected local parse, redacted live observations, source hashes, all diagnostic inputs/outputs, and runnable probes. To reproduce from the repository root, run the Python extraction probe, the Node frontend probe (Node 24 with type stripping), and the Python worker probe. Python probes need the project's import dependencies plus reportlab/Pillow/pdfminer/PyPDF2 for file fixtures. Set ARCHANGEL_REPO to the code checkout if invoking elsewhere. OCR-dependent outcomes must be labeled with environment availability.

The scripts report gaps rather than exiting nonzero for them; they are investigation tools to convert into assertions in the production suite. Detailed expected/actual values are recorded in probe_results.json, frontend_results.json, and worker_race_result.json. Generated PDFs in this folder are test fixtures, not documents to submit as real credentials.

Citation audit and independent-review results are recorded in CV_EXTRACTION_AUDIT_NOTES.md after the final draft check. Re-run the audit after implementation moves lines.
