# PRD: Stable, usable physician onboarding form

Status: implementation-ready; production fix not applied.
Date: 7 September 2026.
Priority: P0 typing/focus stability, followed by P1 usability regressions.
Companion: PRD_CV_EXTRACTION_TO_REVIEW.md. Ship this interaction fix first so physicians can reliably review extracted credentials.

## 1. Problem and desired outcome

A physician must be able to click a field once, type a complete answer, edit the middle of it, select a toggle, and continue without being ejected from the field or unexpectedly moved elsewhere on the page. Completing one field must not reset the rest of the form's state, collapse/expand unrelated sections, or discard unfinished input.

The founder reported that typing a mobile number exits the field after input, and selecting Yes for “Have you finished residency?” moves the page to an unrelated position. This is a technical form defect, not a requirement to turn off assistant browser access: the input replacement and focus loss reproduce in a local simulated DOM with no Chrome control or network access.

The exact pixel scroll jump remains a reported live symptom, not a measured local result. The demonstrated whole-section replacement and accordion-state reset are a strong mechanism for that symptom; browser scrolling must be measured during implementation acceptance.

## 2. Evidence and test scope

Before the user withdrew browser access, read the production Chrome review page and its uploaded-CV result. The page later became “Application received,” so the old application's edit state was no longer used for testing. No field changes, application submission, or new test signup were performed by this investigation. After the user said Chrome access was no longer needed, all investigation stayed in local code and DOM tests.

Downloaded the public landing page's JavaScript asset index-CydTCP5X.js without authentication or application tokens. Its compiled review component contains a section-wrapper function defined inside the parent component and used as a JSX component. The same construct exists in the local source.

Rendered the actual Step5Credentials component and actual primitives/completeness logic using React 18.3.1 and JSDOM 26.1.0, with all network calls disabled. Compared it with a disposable copy that changes only the wrapper component's definition to module scope. This is an isolation experiment, not a production patch.

### Final interaction results

29 diagnostic checks ran on each variant:

- Current source: 8 passed, 21 failed.
- Module-scope wrapper experiment: 26 passed, 3 failed.
- 18 previously failing checks were resolved by stable component identity alone.

Reproduced behavior:

- One phone keystroke replaces the input node and loses active focus. A simulated ten-digit sequence without re-clicking leaves only “2”.
- The same defect affects the non-review credential phase, not just the long review page.
- Residency Yes replaces the clicked button and training section. The boolean value updates, but focus/DOM identity does not survive.
- A manually collapsed identity section reopens after editing another section.
- Textarea, full-string phone input (paste-shaped), Unicode legal-name entry, numeric residency-year input, and a same-value country-select change event replace their controls.
- Editing a CV-populated board field clears its CV marker as intended, but also loses focus.
- A partially typed language chip draft disappears after another credential updates.
- Removing a middle repeated row changes the surviving final row's DOM identity, though its saved text remains correct.

Positive controls: full-string phone input and Unicode values themselves can be stored; the residency boolean updates; removing a row preserves the intended remaining values; accordion collapse/reopen alone preserves an unfinished chip draft; non-submit buttons in the rendered fixture specify button type. The bug is not “all events fail”—parent rerenders replace their descendants.

Three remaining issues after hoisting:

1. The mobile field has no associated programmatic label in the rendered DOM.
2. A completely blank board row counts as filled because its default active=true is treated as content.
3. Repeated rows use array-index keys, so removing an earlier row shifts later row identity.

The phone full-string test sets the value and dispatches an input event; it does not exercise clipboard events. The selector probe dispatches a same-value change; switching countries, asynchronous configuration changes, IME composition, and caret behavior remain required acceptance coverage.

Evidence limitations: JSDOM does not perform layout, native tab-order traversal, screen-reader output, mobile keyboard behavior, or real browser scroll anchoring. Those are required release tests below and are not claimed complete. The diagnostic check count includes related assertions and is not a usability success rate from 29 human participants.

## 3. Root cause and verified code anchors

- `Step5Credentials` — landing/src/app/components/onboarding/steps.tsx:2140. Each field update updates parent credentials and rerenders this function.
- `Group` — landing/src/app/components/onboarding/steps.tsx:2260. Defined inside Step5Credentials, then rendered as a component around all three groups. Each render creates a different component type; React replaces the old subtree.
- `OnboardingSection` — landing/src/app/components/onboarding/primitives.tsx:1575. Open state initializes from defaultOpen. Replacing the parent wrapper remounts the section and resets that local state.
- `TextField` — landing/src/app/components/onboarding/primitives.tsx:439. Controlled input value updates and local focus state are inside the replaced subtree.
- `TextArea` — landing/src/app/components/onboarding/primitives.tsx:663. Same subtree issue.
- `ChipMultiSelect` — landing/src/app/components/onboarding/primitives.tsx:728. Draft text is local state and is lost on replacement.
- `RepeatableCard` — landing/src/app/components/onboarding/steps.tsx:1493. Used by board/training arrays; inspect the index keys at each call site.
- `FieldLabel` — landing/src/app/components/onboarding/primitives.tsx:340. Visible text needs an actual association with each control or group.
- `rowHasContent` — landing/src/app/components/onboarding/completeness.ts:76. Boolean true currently makes an otherwise empty repeated row count as content.
- `reviewSections` — landing/src/app/components/onboarding/completeness.ts:115. Summary groups/weights and opening guidance.
- `checklistRows` — landing/src/app/components/onboarding/completeness.ts:198. Missing/suggested summary copy.

React's official explanation describes the same nested-component reset mechanism: [Preserving and resetting state](https://react.dev/learn/preserving-and-resetting-state). The local controlled experiment, rather than that general guidance alone, establishes its relevance here.

## 4. Design invariants

1. A normal keystroke never unmounts or replaces the focused input, its section, or unrelated sections.
2. A field's value, caret/selection, focused state, and draft text survive unrelated parent updates.
3. A deliberate accordion choice remains a user choice during ordinary editing. Changing completeness must not recalculate open state in a way that fights the physician.
4. Content changes alone do not scroll the window. Only explicit navigation, intentional focus to new content, or a user-requested jump may move it.
5. Empty/unknown is not Yes, No, or completed. Optional information remains optional.
6. Repeatable rows keep stable identity through insert, remove, and reorder. Display position is not identity.
7. Tests exercise real rendered controls and state, not only source strings or a simplified stand-in form.

## 5. Implementation requirements

### P0-A. Stabilize the render tree

Move the section wrapper to module scope with explicit props, or render the stable OnboardingSection directly. Keep the same wrapper/component type and position across field-value changes in review, phase-based credentials, and invited-member rendering. Passing newly created ordinary props is acceptable; creating new component types is not.

Do not patch the symptom by refocusing inputs after every update, remembering a CSS selector and clicking it again, suppressing rerenders with stale memo dependencies, delaying every keystroke, or forcing scroll restoration on all state changes. Those approaches hide lost DOM identity and can break caret, selection, composition input, and accessibility.

Search the entire onboarding component subtree for other nested component definitions rendered as JSX, keys derived from values or random IDs, conditional wrappers that change element type, and effects that focus/scroll on ordinary data changes. Fix confirmed sibling instances within onboarding; avoid unrelated refactors.

### P0-B. Preserve section state and position

Compute initial open sections when entering review: keep required fields and unsupported suggestions discoverable. Once the physician opens/closes a section, persist that choice through typing and toggles. Do not derive live open state solely from completeness after every edit.

Incoming parse suggestions should raise a section-level “new details to review” indicator without stealing focus or scrolling the user away. If automatic expansion is necessary, do it only at the intentional CV-to-review transition. When the physician explicitly jumps to a field, expand its section first and focus it after layout settles.

Do not unmount section bodies merely to collapse them if they contain local drafts. Ensure collapsed content is actually unavailable to keyboard navigation/accessibility while retaining its state. Use consistent section IDs and accessible expanded/controls relationships.

On long review pages, a phone keystroke, residency Yes/No, practice-status change, or CV-chip removal must not reopen an unrelated accordion, restart a whole-section entrance animation, or move scroll to the top. Reserve a single top-of-step scroll/focus for deliberate step navigation.

### P1-A. Stable repeated rows and sensible focus

Give board, fellowship, residency, and future repeatable-license rows stable IDs created once when the row is introduced, not during render. Preserve IDs in drafts or maintain a stable mapping through hydration. Avoid array-index keys.

Adding a row intentionally focuses its first editable control with minimal scroll only if needed. Removing a row leaves unaffected rows' nodes/drafts intact. Focus goes to the nearest remaining relevant control or Add row button if the focused row was removed. Do not jump to the page heading. Removal controls need descriptive names, such as “Remove fellowship 2”, not repeated indistinguishable “Remove” labels.

Coordinate these IDs with the CV extraction PRD's provenance and row-merging schema. Do not implement two conflicting row-ID mechanisms.

### P1-B. Input and accessibility behavior

Associate every visible label with its input/select/textarea using stable IDs and htmlFor, or an equivalent accessible-name relationship. Associate help/error text with aria-describedby; identify toggle groups with a fieldset/legend or an equivalent accessible group. Keep active/unanswered states truthful through aria-pressed/checked semantics.

Support ordinary typing, paste, select-all replacement, backspace/delete, mid-string edits, cursor movement, and Unicode/IME composition. Defer normalization that would disrupt composition/caret; do not silently remove valid punctuation from names or international contact values. Numeric year/NPI constraints may filter invalid input without replacing the control or moving focus.

Keyboard-only users can progress in logical visual order using Tab/Shift+Tab, operate toggles and section headers, and add/remove rows. Enter in a chip input adds that chip, not the application; Enter in a textarea inserts a newline. Never trigger final submission from an incidental editing key. Keep final submission intentional and preserve current validation requirements.

### P1-C. Accurate completeness and clear copy

Fix row completeness with field-specific predicates. A board row needs actual credential content, not its default boolean. Unknown active state remains unanswered; implement its nullable storage/consumer semantics jointly with the extraction PRD.

The number of suggested details must describe what is actually shown. A group chip must not imply blank fellowship specialty or unconfirmed validity was extracted. Use separate counts/copy for “needed to submit” and “optional details that help review.” Do not present a selected subset of missing fields as the only unanswered questions on the page.

Use human-readable structured-review labels (e.g. “Journal peer review”), preserving internal enum values in data. Keep source provenance readable, and use clearly marked examples rather than realistic-looking placeholder answers. These are copy/representation changes, not changes to eligibility or compensation.

### P1-D. Review, save, and async feedback

Typing does not change steps. An upload/poll result or background save does not force the physician back to CV or another part of the form. Preserve manual edits/clears while async results arrive; use the extraction PRD's versioned merge contract rather than another ad hoc patch.

Show “Saving…”/“Saved” only when durable persistence supports the claim. Audit the current “Your progress is saved” promise against what actually survives refresh. If persistence is only step-based, either add deliberate draft persistence with revision handling or narrow the message. Do not make a blanket autosave claim based on local React state. Cover save failure/offline recovery with retained local edits and an actionable retry.

## 6. Tests: required before release

### Automated component suite

Convert the adjacent 29-check diagnostic harness into the normal test runner with independent assertions. Run it against production components, no manual source hoisting in the final tests. All 29 semantic target behaviors must pass after the full PRD, including the three not fixed by the hoisting experiment. Replace diagnostic proxies with robust assertions: check the accessible name (including valid aria-labelledby), not only input.labels.length; assert structured completeness state, not the literal text “0 of”.

Add parameterized coverage for every editable field/control across review mode, each credential phase, invited member, manual/no-CV, and CV-prefilled data. Verify node identity, active element, committed value, draft value, selection range, and section state where relevant. Test functional-state updates under rapid sequential/batched edits; no old closure may erase another field or restore a removed CV chip.

Required examples:

1. Click mobile once; type +1 (202) 555-0147 without re-clicking; all characters remain and focus stays.
2. Focus residency Yes; activate it; section node and focus remain. No unrelated section reopens.
3. Fill residency completion year one digit at a time; edit the middle; caret stays at the intended position.
4. Edit a CV-populated board issuer and a specialty; chips update without replacing inputs.
5. Type an unfinished language chip; change a different field; draft survives. Collapse/reopen also preserves it.
6. Add three board rows; remove the middle; final row node/value/draft remain correctly associated.
7. Change a country selector and reveal international fields without replacing unrelated inputs or losing values. Hidden/cleared fields follow explicit product rules, not accidental remounts.
8. Simulate async config and extraction responses while a field is focused; no focus theft, stale overwrite, section reset, or navigation.
9. Verify associated accessible names, hint relationships, toggle state, descriptive Remove buttons, and a truly empty board row's completeness.

### Real-browser acceptance (still required)

Use an isolated non-production test signup with mock email/OTP/verification, not the already submitted live applicant. Do not infer these results from JSDOM.

Run Chromium and WebKit at desktop and narrow mobile viewports, with 80%, 100%, and 200% desktop zoom where supported; cover at least 390px and 1280px widths and a small laptop height. Use real keyboard events and measure document.activeElement, input node identity, selectionStart/End, window.scrollY, and target bounding rectangle.

- For typing/toggle interactions with no intentional layout insertion, scrollY changes by no more than 2 CSS pixels. Any change from native viewport/keyboard behavior must be separated from application-triggered movement in the report.
- For intended insertion (Add row or explicit field navigation), the target remains visible and unrelated sections do not jump open. Do not apply the 2px rule to a user-requested navigation.
- Reproduce the founder's exact sequence: scroll to residency, click Yes, type the completion year, continue to mobile, and type without re-clicking. Record a trace or screen capture of this isolated test if the test environment permits it.
- Complete the whole signup flow using keyboard-only input and separately pointer input. Exercise validation, back/forward, refresh, failed save/retry, upload retry, and return to an existing draft. Final submission uses synthetic staging data only.
- Check mobile virtual-keyboard visibility, large text, reduced motion, and long CV-derived institution names. No clipped controls, horizontal overflow, obscured active field, or hidden required error.

### Acceptance boundary

The isolated hoisting experiment proves the primary focus/state mechanism; it does not mean production is fixed. Release requires the component suite, real-browser measurements, draft persistence checks, and the extraction PRD's shared async tests. Preserve existing onboarding/terminal-state tests and CI shard coverage.

## 7. Do not touch / non-goals

No production application edits/submission, new real accounts, changes to approval or tier weights, legal attestations, unrelated auth/product flows, bulk-media work, database deletion, or extraction algorithm rewrite in the P0 focus patch. Coordinate shared field-ID, nullable status, provenance, and persistence work with the companion PRD rather than duplicating it.

No dependency upgrade is required to fix component identity. The diagnostic packages were installed only in a temporary test directory. No Chrome access is required for implementing or running local component tests.

## 8. Implementation and handoff order

1. Add failing rendered-component tests for typing and residency toggle. Hoist/stabilize the wrapper and check all modes. This should be a small, reviewable P0 change.
2. Add stable row IDs and accessible associations, correct completeness/copy, and verify persistence feedback.
3. Run the isolated real-browser matrix and shared extraction/async tests. Resolve failures with evidence before claiming success.
4. Audit citations and get independent review. Include exact test counts, environments, browser scroll measurements, and remaining limitations in the PR description.

Evidence lives in onboarding-ux-evidence: current/stable JSON results, the source/bundle fingerprint, a public compiled snippet establishing the same wrapper pattern, and reproducible build/run scripts. The scripts generate disposable component copies; never replace production files with those generated copies. The module-scope variant is a root-cause experiment, not a complete implementation.
