/* Where a doctor is licensed, asserted against the real screens.
 *
 * A GMC-registered consultant wrote to us: "Tried but the 'Outside the US' is
 * not allowed to be selected in registration so I cannot proceed. I am based
 * in UK". Screen 1 passed "Outside the US" as the select's PLACEHOLDER, and
 * SelectField renders every placeholder as a disabled option, so the one
 * honest answer they had was the only entry they could not click.
 *
 * These are BEHAVIOURAL. The Python suites assert the same rules by reading
 * this source as text, which is the repo's convention, and a source-literal
 * assertion has already gone red on a reformat once and survived a seeded bug
 * once. Mounting the real component is what actually distinguishes the two.
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

/* Every fetch is recorded AND refused. Screen 1 asks for the country list, and
   whether it asks at all is one of the properties below. */
const fetched = [];
global.fetch = async (url) => { fetched.push(String(url)); throw new Error("network disabled"); };

const React = require(path.join(DEPS, "react"));
const { act } = React;

const { bundlePath, step1BundlePath } = require("./build-onboarding-fixture.cjs");
const { mount } = require(bundlePath);
const { mountStep1 } = require(step1BundlePath);

// ── harness ─────────────────────────────────────────────────────────────────

let ctrl = null;

async function screenOne(options = {}) {
  if (ctrl) await act(async () => ctrl.unmount());
  document.getElementById("root").innerHTML = "";
  fetched.length = 0;
  await act(async () => { ctrl = mountStep1(document.getElementById("root"), options); });
  return ctrl;
}

async function review(options = {}) {
  if (ctrl) await act(async () => ctrl.unmount());
  document.getElementById("root").innerHTML = "";
  fetched.length = 0;
  await act(async () => { ctrl = mount(document.getElementById("root"), options); });
  return ctrl;
}

const nativeSelectValue =
  Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, "value").set;

/** A real pick from a <select>, the way React sees one. */
async function choose(select, value) {
  assert.ok(select, "no select to choose from");
  await act(async () => {
    nativeSelectValue.call(select, value);
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

/* Selects are identified by WHAT THEY OFFER rather than by their label text.
   A label is a string somebody will reword, and the optional marker renders
   inside it, which already made a label-matching helper return null for a
   field that was plainly on the page. What a control offers is the thing the
   test actually cares about. */
const optionValues = (sel) => [...sel.options].map((o) => o.value);
const allSelects = () => [...document.querySelectorAll("select")];

/** A country list: it offers Zimbabwe. No US state does. */
const countrySelects = () => allSelects().filter((s) => optionValues(s).includes("ZW"));
/** The US state list: Wyoming, and no Zimbabwe. */
const stateSelect = () =>
  allSelects().find((s) => {
    const v = optionValues(s);
    return v.includes("WY") && !v.includes("ZW");
  }) || null;

/** The nth country select on the page, in DOM order. Screen 1 has one. The
 *  Review screen has two: practise, then licensure. */
const countrySelect = () => countrySelects()[countrySelects().length - 1] || null;
const practiseSelect = () => countrySelects()[0] || null;
const licensureSelect = () => countrySelects()[countrySelects().length - 1] || null;

// ── the dead end itself ─────────────────────────────────────────────────────

test("a doctor outside the US has an answer they can actually select", async () => {
  const c = await screenOne();
  const select = countrySelect();
  assert.ok(select, "screen 1 asks no country question");

  const gb = [...select.options].find((o) => o.value === "GB");
  assert.ok(gb, "the United Kingdom is not on the list");
  assert.equal(gb.disabled, false, "the UK option is disabled, which is the original bug");

  await choose(select, "GB");
  assert.equal(c.data.credentials.countryOfLicensure, "GB");
});

test("the US state picker is only shown to a doctor licensed in the US", async () => {
  const c = await screenOne();
  assert.ok(stateSelect(), "a US physician is not offered a state");
  await choose(countrySelect(), "GB");
  assert.equal(stateSelect(), null, "a UK consultant is still asked for a US state");
  await choose(countrySelect(), "US");
  assert.ok(stateSelect(), "the state question did not come back");
});

test("the state picker is optional and can be set back to no answer", async () => {
  const c = await screenOne();
  await choose(stateSelect(), "CA");
  assert.equal(c.data.credentials.licenseState, "CA");
  const blank = [...stateSelect().options].find((o) => o.value === "");
  assert.equal(blank.disabled, false, "an optional field that cannot be cleared is not optional");
  await choose(stateSelect(), "");
  assert.equal(c.data.credentials.licenseState, "");
});

test("neither the country nor the state can disable Continue", async () => {
  const c = await screenOne();
  const cont = [...document.querySelectorAll("button")]
    .find((b) => b.textContent.startsWith("Continue"));
  assert.equal(cont.disabled, false);
  await choose(countrySelect(), "GB");
  assert.equal(cont.disabled, false, "picking a country outside the US killed Continue");
});

// ── the mirror rule ─────────────────────────────────────────────────────────

test("the practice and degree countries follow the first answer", async () => {
  const c = await screenOne();
  await choose(countrySelect(), "GB");
  assert.equal(c.data.credentials.countryOfPractice, "GB");
  assert.equal(c.data.credentials.countryOfDegree, "GB");
});

test("correcting a mis-clicked country corrects practice and degree with it", async () => {
  // The defect: under "fill only a blank", the second pick moved licensure and
  // left practice and degree on the first one. country_of_practice is what
  // renders the physician's card and the verification queue row, so the doctor
  // would be published as practising somewhere they do not.
  const c = await screenOne();
  await choose(countrySelect(), "GB");
  await choose(countrySelect(), "US");
  assert.equal(c.data.credentials.countryOfLicensure, "US");
  assert.equal(c.data.credentials.countryOfPractice, "US", "practice was stranded in GB");
  assert.equal(c.data.credentials.countryOfDegree, "US", "degree was stranded in GB");
});

test("a practice country that deliberately differs is never overwritten", async () => {
  const c = await screenOne({ credentials: {
    countryOfLicensure: "GB", countryOfPractice: "FR", countryOfDegree: "IE" } });
  await choose(countrySelect(), "US");
  assert.equal(c.data.credentials.countryOfLicensure, "US");
  assert.equal(c.data.credentials.countryOfPractice, "FR");
  assert.equal(c.data.credentials.countryOfDegree, "IE");
});

test("practice and degree are never left blank by a change", async () => {
  const c = await screenOne();
  for (const code of ["GB", "IN", "AU", "US", "SA"]) {
    await choose(countrySelect(), code);
    assert.equal(c.data.credentials.countryOfPractice, code);
    assert.equal(c.data.credentials.countryOfDegree, code);
  }
});

// ── the licence that must not travel ────────────────────────────────────────

test("a US licence is dropped when the country stops being the US", async () => {
  // Hidden is not enough: the Review screen stops RENDERING these, and the
  // credentials blob is posted whole. credentials.py turns a non-empty
  // licenseState into `state_licensed: true` and a US medical-board lookup
  // handle in the block shipped to buyers.
  const c = await screenOne({ credentials: {
    countryOfLicensure: "US", licenseState: "CA", licenseNumber: "A12345" } });
  await choose(countrySelect(), "GB");
  assert.equal(c.data.credentials.licenseState, "");
  assert.equal(c.data.credentials.licenseNumber, "");
});

test("choosing the US does not clear a licence", async () => {
  // The other half of the rule: the clearing is CONDITIONAL. Starting from a
  // stale GB-with-a-US-licence pair and correcting the country to US has to
  // leave the licence alone, or a US physician loses their own details to a
  // mis-click they then corrected.
  const c = await screenOne({ credentials: {
    countryOfLicensure: "GB", licenseState: "CA", licenseNumber: "A12345" } });
  await choose(countrySelect(), "US");
  assert.equal(c.data.credentials.licenseState, "CA");
  assert.equal(c.data.credentials.licenseNumber, "A12345");
});

test("the Review screen drops the licence when the licensure country changes", async () => {
  const c = await review({ credentials: {
    countryOfPractice: "US", countryOfLicensure: "US",
    licenseState: "CA", licenseNumber: "A12345" } });
  await choose(licensureSelect(), "GB");
  assert.equal(c.data.credentials.licenseState, "");
  assert.equal(c.data.credentials.licenseNumber, "");
});

test("the Review screen drops the licence when the PRACTISE control moves licensure", async () => {
  // This control writes countryOfLicensure too, while the two still agree, so
  // it has to clear the licence for exactly the same reason.
  const c = await review({ credentials: {
    countryOfPractice: "US", countryOfLicensure: "US",
    licenseState: "CA", licenseNumber: "A12345" } });
  await choose(practiseSelect(), "GB");
  assert.equal(c.data.credentials.countryOfLicensure, "GB");
  assert.equal(c.data.credentials.licenseState, "", "a US state survived on a GB doctor");
  assert.equal(c.data.credentials.licenseNumber, "");
});

test("the US licence fields are not rendered to a doctor licensed elsewhere", async () => {
  await review({ credentials: { countryOfPractice: "GB", countryOfLicensure: "GB" } });
  assert.equal(document.querySelector('input[placeholder="MA"]'), null,
               'the free-text "Licence state" is still on the page');
  assert.equal(document.querySelector('input[placeholder="MD-99881"]'), null);
});

// ── which doors ask, and which fetch ────────────────────────────────────────

test("only the physician door asks for the country list", async () => {
  await screenOne({ kind: "advisor" });
  assert.equal(countrySelect(), null, "an advisor is asked where they are licensed");
  assert.equal(fetched.filter((u) => u.includes("credential-config")).length, 0,
               "an advisor fetched a country list their door never shows");

  await screenOne({ kind: "physician" });
  assert.ok(countrySelect());
  assert.equal(fetched.filter((u) => u.includes("credential-config")).length, 1);
});

test("the country list still works with the network down", async () => {
  // Every fetch in this file throws. The offline fallback is built from
  // countries.json, so a failed config request costs plainer labels and never
  // a dropdown the doctor's own country is missing from.
  await screenOne();
  const values = [...countrySelect().options].map((o) => o.value);
  for (const code of ["US", "GB", "IN", "SA", "NG", "AU"]) {
    assert.ok(values.includes(code), `${code} is missing with the config fetch failing`);
  }
});

test("a select that must be answered keeps its blank disabled", async () => {
  // The other direction of `disabled={!optional}`, and the one that matters:
  // without it every placeholder becomes selectable, which is how a country
  // question stops being a question. Deleting the attribute outright passed
  // every other test in this file.
  await screenOne();
  const blank = [...countrySelect().options].find((o) => o.value === "");
  assert.ok(blank, "the country select has no placeholder option");
  assert.equal(blank.disabled, true,
               "the country placeholder is selectable, so the answer is optional");
});

test("a required blank stays disabled on the Review screen too", async () => {
  await review({ credentials: { countryOfPractice: "", countryOfLicensure: "" } });
  for (const sel of countrySelects()) {
    const blank = [...sel.options].find((o) => o.value === "");
    assert.equal(blank.disabled, true, "a Review country select can be blanked");
  }
});
