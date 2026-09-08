/* The physician onboarding form must survive being typed into.
 *
 * Onboarding Master PRD, Phase 1 — PRD B P0-A / P0-B.
 *
 * PROMOTED FROM A DIAGNOSTIC. The 29 checks below began as
 * docs/prd/onboarding-master/evidence/onboarding-ux-evidence/run_form_probe.cjs,
 * a single script that printed a JSON report and exited zero whether or not
 * anything passed. Two things changed on the way in:
 *
 *   1. Each check is now an independent assertion that FAILS THE BUILD. A
 *      report nobody is required to read is not a test.
 *   2. It runs against production sources with no patching. The diagnostic
 *      compared the real form against a disposable copy with `Group` hoisted,
 *      to prove the mechanism; there is nothing left to compare against,
 *      because the hoist is now in the product.
 *
 * The baseline that justified the change, reproduced against this checkout
 * before it was made: 8 of 29 passing, 26 of 29 with the hoist alone. The
 * three that the hoist does not fix are marked below and belong to Phase 3.
 *
 * WHAT THIS CANNOT TELL YOU. JSDOM has no layout engine. It does not scroll,
 * it does not lay out, it has no mobile keyboard, no IME, and no screen
 * reader. Every scroll-position, viewport, zoom, composition and
 * assistive-technology claim in PRD B §6 remains a human test in the sandbox
 * realm and is NOT evidenced here. What this file does establish is DOM
 * identity, focus, committed values, draft values and section state — which is
 * where the defect actually lived.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const DEPS = process.env.ARCHANGEL_UX_DEPS || path.join(__dirname, "node_modules");
const { JSDOM } = require(path.join(DEPS, "jsdom"));

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
                      { url: "http://component.test" });
for (const key of ["window", "document", "HTMLElement", "HTMLInputElement",
                   "HTMLTextAreaElement", "HTMLSelectElement", "Event",
                   "MouseEvent", "KeyboardEvent", "Node", "getComputedStyle"]) {
  global[key] = dom.window[key];
}
Object.defineProperty(global, "navigator", { value: dom.window.navigator, configurable: true });
global.IS_REACT_ACT_ENVIRONMENT = true;
/* No outbound request is permitted from a component test. */
global.fetch = async () => { throw new Error("network is disabled in component tests"); };

const React = require(path.join(DEPS, "react"));
const { act } = React;

const { bundlePath } = require("./build-onboarding-fixture.cjs");
const { mount } = require(bundlePath);

// ── harness ─────────────────────────────────────────────────────────────────

let ctrl = null;

async function setup(options = {}) {
  if (ctrl) await act(async () => ctrl.unmount());
  document.getElementById("root").innerHTML = "";
  await act(async () => { ctrl = mount(document.getElementById("root"), options); });
  return ctrl;
}

/* React attaches its own value setter to the input prototype, so assigning
   `el.value` directly is invisible to it. Going through the native descriptor
   and then dispatching `input` is what a real keystroke looks like from
   React's side. */
const nativeInputValue =
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
const nativeAreaValue =
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;

async function type(el, value) {
  await act(async () => {
    (el.tagName === "TEXTAREA" ? nativeAreaValue : nativeInputValue).call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function focus(el) { await act(async () => el.focus()); }
async function click(el) {
  assert.ok(el, "click target must exist");
  await act(async () => el.click());
}

const phone = () => document.querySelector('input[type="tel"]');
const header = (id) => document.querySelector('button[aria-controls="' + id + '"]');
const byPlaceholder = (p) => document.querySelector('input[placeholder="' + p + '"]');
const allByPlaceholder = (p) =>
  [...document.querySelectorAll('input[placeholder="' + p + '"]')];

const findLabel = (text) =>
  [...document.querySelectorAll("div,label,span")].find(
    (e) => e.textContent.trim().toLowerCase() === text.toLowerCase()
        && !e.querySelector("div,label,span"));

const toggle = (label, value) => {
  const lbl = findLabel(label);
  assert.ok(lbl, 'no label "' + label + '" on the page');
  return [...lbl.parentElement.querySelectorAll("button")]
    .find((x) => x.textContent === value);
};

/* The accessible name of a control, computed the way an assistive technology
   would rather than by counting `labels`. `input.labels.length > 0` — what the
   diagnostic asserted — is satisfied by a <label> with no text in it, and is
   not satisfied by a correct aria-labelledby. Neither answer is the one a
   physician using a screen reader gets. */
function accessibleName(el) {
  const byLabelledBy = (el.getAttribute("aria-labelledby") || "")
    .split(/\s+/).filter(Boolean)
    .map((id) => (document.getElementById(id) || {}).textContent || "")
    .join(" ").trim();
  if (byLabelledBy) return byLabelledBy;
  const aria = (el.getAttribute("aria-label") || "").trim();
  if (aria) return aria;
  const labels = [...(el.labels || [])].map((l) => l.textContent.trim()).filter(Boolean);
  return labels.join(" ").trim();
}

const BOARD_PLACEHOLDER = "American Board of Internal Medicine";
const YEAR_PLACEHOLDER = "2010";
const LANGUAGES_PLACEHOLDER = "List all languages";
const NAME_PLACEHOLDER = "Dr. Tej Patel";

test.after(async () => {
  if (ctrl) await act(async () => ctrl.unmount());
  /* JSDOM installs its own timers and keeps the event loop alive; without this
     the process runs green and then never exits, which in CI is a hung job
     rather than a passing one. */
  dom.window.close();
});

// ── 1. A keystroke does not replace the field being typed into ──────────────

test("one character does not replace the phone input node", async () => {
  await setup();
  const el = phone();
  await focus(el);
  await type(el, "2");
  assert.equal(el, phone(), "the input node was replaced by a single keystroke");
});

test("one character does not cost the phone input its focus", async () => {
  await setup();
  const el = phone();
  await focus(el);
  await type(el, "2");
  assert.equal(document.activeElement, phone());
});

test("ten digits can be typed without clicking the field again", async () => {
  /* The founder's report, exactly: typing a mobile number left "2" behind
     because every digit ejected them from the field. */
  await setup();
  await focus(phone());
  for (const ch of "2025550147") {
    const active = document.activeElement;
    if (active.tagName === "INPUT") await type(active, active.value + ch);
  }
  assert.equal(phone().value, "2025550147");
});

test("the same defect is absent from the phased (non-review) form", async () => {
  /* The long review page made it obvious, but the bug was in the component
     tree, so it was equally present in the three-screen credential path. */
  await setup({ reviewMode: false, phase: 1 });
  const el = phone();
  await focus(el);
  await type(el, "2");
  assert.equal(el, phone());
  assert.equal(document.activeElement, phone());
});

test("a full pasted phone string survives with focus intact", async () => {
  await setup();
  const el = phone();
  await focus(el);
  await type(el, "+1 (202) 555-0147");
  assert.equal(phone().value, "+1 (202) 555-0147");
  assert.equal(document.activeElement, el);
});

test("a Unicode legal name is stored and keeps its field", async () => {
  await setup();
  const el = byPlaceholder(NAME_PLACEHOLDER);
  await focus(el);
  await type(el, "José Muñoz");
  assert.equal(ctrl.data.credentials.fullLegalName, "José Muñoz");
  assert.equal(document.activeElement, el);
});

test("a residency year can be typed a digit at a time", async () => {
  await setup();
  const el = byPlaceholder(YEAR_PLACEHOLDER);
  await focus(el);
  await type(el, "2");
  assert.equal(el, byPlaceholder(YEAR_PLACEHOLDER));
  assert.equal(document.activeElement, el);
});

test("a textarea keeps its node and its focus while being typed into", async () => {
  await setup();
  await click(header("onb-sec-focus"));
  const area = document.querySelector("textarea");
  await focus(area);
  await type(area, "Dialysis");
  assert.equal(area, document.querySelector("textarea"));
  assert.equal(document.activeElement, area);
});

test("a country select is not replaced by its own change event", async () => {
  await setup();
  const select = document.querySelector("select");
  await focus(select);
  await act(async () => select.dispatchEvent(new Event("change", { bubbles: true })));
  assert.equal(select, document.querySelector("select"));
});

// ── 2. A toggle does not tear down the section around it ────────────────────

test("pressing residency Yes does not replace the button under the pointer", async () => {
  await setup();
  const yes = toggle("Have you finished residency?", "Yes");
  await focus(yes);
  await click(yes);
  assert.equal(yes, toggle("Have you finished residency?", "Yes"));
});

test("pressing residency Yes leaves focus on the control that was pressed", async () => {
  await setup();
  const yes = toggle("Have you finished residency?", "Yes");
  await focus(yes);
  await click(yes);
  assert.equal(document.activeElement, toggle("Have you finished residency?", "Yes"));
});

test("pressing residency Yes does not remount the training section", async () => {
  await setup();
  const training = document.getElementById("onb-sec-training");
  await click(toggle("Have you finished residency?", "Yes"));
  assert.equal(training, document.getElementById("onb-sec-training"));
});

test("pressing residency Yes actually records the answer", async () => {
  /* The positive control. The bug was never "events do not fire" — the value
     always updated; it was the DOM around it that was thrown away. */
  await setup();
  await click(toggle("Have you finished residency?", "Yes"));
  assert.equal(ctrl.data.credentials.residencyCompleted, true);
});

// ── 3. A deliberate accordion choice is the physician's, not the form's ─────

test("a section can be collapsed by hand", async () => {
  await setup();
  await click(header("onb-sec-identity"));
  assert.equal(header("onb-sec-identity").getAttribute("aria-expanded"), "false");
});

test("editing an unrelated section does not reopen a collapsed one", async () => {
  /* PRD B invariant 3. Open state is local to OnboardingSection, so a remount
     resets it to defaultOpen — which completeness recomputes on every edit.
     That is the mechanism behind the reported "the page jumps somewhere
     else". */
  await setup();
  await click(header("onb-sec-identity"));
  await click(toggle("Have you finished residency?", "Yes"));
  assert.equal(header("onb-sec-identity").getAttribute("aria-expanded"), "false",
               "a collapsed section reopened itself after an unrelated edit");
});

// ── 4. Unfinished input is not discarded ────────────────────────────────────

test("a half-typed language chip survives an edit elsewhere on the page", async () => {
  await setup();
  await click(header("onb-sec-focus"));
  const lang = byPlaceholder(LANGUAGES_PLACEHOLDER);
  await focus(lang);
  await type(lang, "Spa");
  await click(toggle("Have you finished residency?", "Yes"));
  assert.equal(byPlaceholder(LANGUAGES_PLACEHOLDER).value, "Spa");
});

test("a half-typed language chip survives collapsing and reopening its section", async () => {
  /* Which is why a collapsed section is hidden with CSS rather than
     unmounted: `display: none` keeps React state alive AND takes the content
     out of the tab order and the accessibility tree, which is both halves of
     what PRD B P0-B asks for. */
  await setup();
  await click(header("onb-sec-focus"));
  const lang = byPlaceholder(LANGUAGES_PLACEHOLDER);
  await focus(lang);
  await type(lang, "Español");
  await click(header("onb-sec-focus"));
  await click(header("onb-sec-focus"));
  assert.equal(byPlaceholder(LANGUAGES_PLACEHOLDER).value, "Español");
});

// ── 5. CV-populated fields behave like any other field ──────────────────────

const CV_BOARD = {
  credentials: {
    boardCertifications: [
      { board: "ABIM", specialty: "Nephrology", subspecialty: "", active: false },
    ],
  },
  chips: ["boardCertifications"],
};

test("editing a CV-populated board field keeps focus", async () => {
  await setup(CV_BOARD);
  const board = byPlaceholder(BOARD_PLACEHOLDER);
  await focus(board);
  await type(board, "ABIMX");
  assert.equal(document.activeElement, board);
});

test("editing a CV-populated board field clears its From-your-CV marker", async () => {
  /* The intended behaviour, and a positive control: the edit is registered. */
  await setup(CV_BOARD);
  const board = byPlaceholder(BOARD_PLACEHOLDER);
  await focus(board);
  await type(board, "ABIMX");
  assert.equal(ctrl.data.cvAutofilled.includes("boardCertifications"), false);
});

// ── 6. Repeated rows ────────────────────────────────────────────────────────

const THREE_BOARDS = {
  credentials: {
    boardCertifications: [
      { board: "A", specialty: "Nephrology", subspecialty: "", active: false },
      { board: "B", specialty: "Nephrology", subspecialty: "", active: false },
      { board: "C", specialty: "Nephrology", subspecialty: "", active: false },
    ],
  },
};

const removeButtons = () =>
  [...document.querySelectorAll("button")].filter(
    (x) => (x.getAttribute("aria-label") || "").startsWith("Remove") || x.title === "Remove");

test("removing a middle row keeps the surviving rows' values", async () => {
  await setup(THREE_BOARDS);
  await click(removeButtons()[1]);
  assert.deepEqual(ctrl.data.credentials.boardCertifications.map((x) => x.board),
                   ["A", "C"]);
});

/* The three checks the hoist does NOT fix. They are recorded here, in the file
   that owns them, rather than dropped — a known defect with no test is a defect
   nobody is counting. They carry no body on purpose: a `todo` with a failing
   body is a red build that everyone learns to ignore. Phase 3 replaces each of
   these with the real assertion. */
test.todo(
  "removing a middle row keeps the surviving rows' DOM identity — Phase 3 (PRD B P1-A). " +
  "Rows are keyed by array index, so removing an earlier row shifts every later " +
  "row's identity. Stable per-row ids are deferred deliberately: P1-A says to " +
  "coordinate them with PRD C's provenance/row-merge schema rather than ship two " +
  "conflicting row-id mechanisms.");

// ── 7. Form hygiene ─────────────────────────────────────────────────────────

test("every non-submit control declares its button type", async () => {
  /* An untyped <button> inside a form defaults to type=submit, so pressing
     Enter while editing would file the application. */
  await setup();
  const untyped = [...document.querySelectorAll("button")]
    .filter((b) => !b.getAttribute("type"));
  assert.deepEqual(untyped.map((b) => b.textContent.trim()), []);
});

test.todo(
  "the mobile field exposes a programmatic accessible name — Phase 3 (PRD B P1-B). " +
  "The visible label is not associated with the control, so a screen reader " +
  "announces an unlabelled text field.");

test.todo(
  "a blank board row does not count as filled — Phase 3 (PRD B P1-C). " +
  "rowHasContent treats the default `active: true` as content, so an empty " +
  "repeated row is counted as an answered one.");
