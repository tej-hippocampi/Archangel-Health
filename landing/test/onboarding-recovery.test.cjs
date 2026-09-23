"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const deps = process.env.ARCHANGEL_UX_DEPS || path.join(__dirname, "node_modules");
const { JSDOM } = require(path.join(deps, "jsdom"));
const esbuild = require(path.join(deps, "esbuild"));
const dom = new JSDOM('<div id="root"></div>', { url: "http://component.test/onboard/recovery-token" });
for (const key of ["window", "document", "HTMLElement", "HTMLInputElement", "Event", "MouseEvent", "Node", "getComputedStyle"]) {
  global[key] = dom.window[key];
}
Object.defineProperty(global, "navigator", { value: dom.window.navigator, configurable: true });
global.IS_REACT_ACT_ENVIRONMENT = true;
const React = require(path.join(deps, "react"));
const { act } = React;
const { createRoot } = require(path.join(deps, "react-dom/client"));
const out = fs.mkdtempSync(path.join(os.tmpdir(), "onboarding-recovery-test-"));
fs.symlinkSync(path.resolve(deps), path.join(out, "node_modules"), "dir");
const src = path.resolve(__dirname, "../src");
esbuild.buildSync({
  stdin: { contents: `export {default as Wizard} from './app/components/OnboardingWizard';
    export {StepApplicationSubmitted as Submitted} from './app/components/onboarding/steps';
    export {default as Reset} from './app/components/ResetPasswordPage';`, resolveDir: src },
  bundle: true, platform: "node", format: "cjs", jsx: "automatic",
  outfile: path.join(out, "components.cjs"),
  external: ["react", "react-dom", "react-dom/client", "react/jsx-runtime"],
  alias: { "@/lib/auth-api": path.join(src, "lib/auth-api.ts"), "@/lib/npi": path.join(src, "lib/npi.ts") },
  define: { "import.meta.env": '{"DEV":true}' }, logLevel: "silent",
});
const { Wizard, Reset, Submitted } = require(path.join(out, "components.cjs"));
let root;
test.afterEach(async () => { if (root) await act(async () => root.unmount()); root = null; });
test.after(() => { dom.window.close(); fs.rmSync(out, { recursive: true, force: true }); });
async function mount(Component, props) {
  window.sessionStorage.clear();
  window.history.replaceState({}, "", "/onboard/recovery-token");
  root = createRoot(document.getElementById("root"));
  await act(async () => root.render(React.createElement(Component, props)));
}
async function type(input, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function submit() {
  await act(async () => document.querySelector("form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
}

test("advisor submission explains manual review without an exam", async () => {
  await mount(Submitted, { data: { firstName: "Test", lastName: "Advisor" }, advisor: true });
  assert.match(document.body.textContent, /Reviewer access opens after approval/);
  assert.ok([...document.querySelectorAll("button")].some(b => b.textContent.includes("Check my application status")));
  assert.doesNotMatch(document.body.textContent, /examination|practice case|Dr\./i);
});

test("physician submission still leads to the examination", async () => {
  await mount(Submitted, { data: { firstName: "Test", lastName: "Physician" } });
  assert.match(document.body.textContent, /examination/i);
  assert.doesNotMatch(document.body.textContent, /Check my application status/);
});

test("an invalid onboarding link offers physician signup and existing-account sign in", async () => {
  global.fetch = async () => new Response(JSON.stringify({ detail: "Invalid or expired onboarding link." }), { status: 404 });
  await mount(Wizard, { token: "recovery-token" });
  assert.match(document.body.textContent, /Invalid or expired onboarding link/);
  assert.equal(document.querySelector('a[href="/join"]').textContent, "Start with a new onboarding link");
  assert.ok([...document.querySelectorAll("button")].some(b => b.textContent === "Sign in"));
});

test("a temporary session failure retries the same link", async () => {
  const calls = [];
  global.fetch = async (url) => {
    calls.push(url);
    return calls.length === 1
      ? new Response(JSON.stringify({ detail: "Please try again." }), { status: 503 })
      : new Response(JSON.stringify({ status: "pending", product: "asclepius", step: 0,
          director_first_name: "Asha", director_email: "doctor@aiimsjodhpur.edu.in" }));
  };
  await mount(Wizard, { token: "recovery-token" });
  const retry = [...document.querySelectorAll("button")].find(b => b.textContent === "Try again");
  await act(async () => retry.click());
  // Only the SESSION calls. Screen 1 also fetches the country list, which is a
  // different endpoint on a different schedule, and the property under test is
  // that the failed session load is retried with the same link — not that the
  // wizard makes exactly two requests in total.
  const sessions = calls.filter(u => String(u).includes("/api/onboarding/session"));
  assert.equal(sessions.length, 2);
  assert.equal(sessions[0], sessions[1]);
  assert.equal(document.querySelector('input[type="email"]').value, "doctor@aiimsjodhpur.edu.in");
  assert.doesNotMatch(document.body.textContent, /This onboarding link can't be loaded/);
});

test("an invalid member invitation does not start an unrelated physician signup", async () => {
  global.fetch = async () => new Response(JSON.stringify({ detail: "Invalid invitation." }), { status: 404 });
  await mount(Wizard, { token: "recovery-token", mode: "member" });
  assert.equal(document.querySelector('a[href="/join"]'), null);
});

test("a returning applicant can request a password link without a reset token", async () => {
  const calls = [];
  global.fetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ message: "Check your email for a password link." }));
  };
  await mount(Reset, { token: "" });
  assert.equal(document.querySelectorAll('input[type="password"]').length, 0);
  await type(document.querySelector('input[type="email"]'), "returning@example.org");
  await submit();
  assert.deepEqual(calls, [{ url: "/api/asclepius/auth/password/forgot", body: { email: "returning@example.org" } }]);
  assert.match(document.querySelector('[role="status"]').textContent, /Check your email/);
});

test("a refused reset request shows the failure and permits retry", async () => {
  global.fetch = async () => new Response(JSON.stringify({ detail: "Too many requests. Try again later." }), { status: 429 });
  await mount(Reset, { token: "" });
  await type(document.querySelector('input[type="email"]'), "returning@example.org");
  await submit();
  assert.match(document.querySelector('[role="alert"]').textContent, /Too many requests/);
  assert.equal(document.querySelector('[role="status"]'), null);
  assert.equal(document.querySelector('button[type="submit"]').disabled, false);
});

test("an unfinished passwordless application resumes where it can choose a password", async () => {
  global.fetch = async (url) => {
    assert.match(url, /\/api\/onboarding\/session\?/);
    return new Response(JSON.stringify({
      status: "pending", product: "asclepius", step: 2,
      director_first_name: "Amara", director_last_name: "Okafor", director_email: "returning@example.org",
      director_password_set: false,
      director_credentials: { fullLegalName: "Amara Okafor", primarySpecialty: "Nephrology" },
      director_attestations: { signedInitials: "AO" },
    }));
  };
  await mount(Wizard, { token: "recovery-token" });
  assert.equal(document.querySelectorAll('input[type="password"]').length, 2);
  assert.match(document.body.textContent, /Choose a password/);
  assert.equal(document.querySelector('input[type="email"]').value, "returning@example.org");
});

const parsedCv = { ok: true, specialty_display: "Nephrology", mobile_phone: "+1 415 555 0101", degrees: ["MD"] };
function cvSession(stage, credentials = {}) {
  return {
    status: "pending", product: "asclepius", step: 2,
    director_first_name: "Amara", director_last_name: "Okafor", director_email: "returning@example.org",
    director_password_set: true,
    director_credentials: { cvAssetSha: "a".repeat(64), cvFilename: "cv.pdf", cvAttemptId: "attempt-one", ...credentials },
    director_cv: { uploaded: true, filename: "cv.pdf", stage, parsed: stage === "done" ? parsedCv : null },
  };
}
const specialtyInput = () => document.querySelector('input[placeholder="Your primary clinical specialty"]');
const phoneInput = () => document.querySelector('input[type="tel"]');

test("resuming a parsed CV restores unsaved suggestions and retains physician edits", async () => {
  global.fetch = async (url) => new Response(JSON.stringify(url.includes("/session?")
    ? cvSession("done", { phone: "+44 20 5555 0101", cvManualFields: ["phone"] })
    : { countries: [] }));
  await mount(Wizard, { token: "recovery-token" });
  assert.equal(specialtyInput().value, "Nephrology");
  assert.equal(phoneInput().value, "+44 20 5555 0101");
});

for (const manual of [false, true]) {
  test(`resuming failed replacement clears old suggestions and preserves manual choice (${manual})`, async () => {
    global.fetch = async (url) => new Response(JSON.stringify(url.includes("/session?")
      ? cvSession("failed", { primarySpecialty: manual ? "Dermatology" : "Nephrology",
          cvSuggestions: { primarySpecialty: "Nephrology" },
          cvManualFields: manual ? ["primarySpecialty"] : [] })
      : { countries: [] }));
    await mount(Wizard, { token: "recovery-token" });
    if (manual) {
      // A confirmed manual answer resumes the attestation screen; returning
      // to Review must still show that answer after a failed replacement.
      const back = [...document.querySelectorAll("button")].find(b => /Back/.test(b.textContent));
      await act(async () => back.click());
    }
    assert.equal(specialtyInput().value, manual ? "Dermatology" : "");
  });
}

for (const ambiguous of [false, true]) {
  test(`replacement specialty requires Review again on reload (${ambiguous})`, async () => {
    const session = cvSession("done", { primarySpecialty: "Nephrology",
      cvSuggestions: { primarySpecialty: "Nephrology" } });
    session.director_cv.parsed = { ok: true,
      specialty: ambiguous ? null : "dermatology",
      specialty_display: ambiguous ? "" : "Dermatology",
      specialty_status: ambiguous ? "ambiguous" : "resolved" };
    global.fetch = async (url) => new Response(JSON.stringify(url.includes("/session?") ? session : { countries: [] }));
    await mount(Wizard, { token: "recovery-token" });
    assert.equal(specialtyInput().value, ambiguous ? "" : "Dermatology");
    assert.match(document.body.textContent, /Review the fields/);
    if (ambiguous) assert.match(document.body.textContent, /Your CV lists more than one specialty/);
  });
}

test("resuming an unfinished CV parse polls again without replacing an in-progress edit", async () => {
  let polls = 0;
  global.fetch = async (url) => {
    if (url.includes("/session?")) return new Response(JSON.stringify(cvSession("reading")));
    if (url.includes("/cv/status?")) {
      polls++;
      return new Response(JSON.stringify({ stage: "done", finished: true, attempt_id: "attempt-one", parsed: parsedCv }));
    }
    return new Response(JSON.stringify({ countries: [] }));
  };
  await mount(Wizard, { token: "recovery-token" });
  await type(phoneInput(), "+44 20 5555 0199");
  await act(async () => new Promise((resolve) => setTimeout(resolve, 1000)));
  assert.equal(polls, 1);
  assert.equal(specialtyInput().value, "Nephrology");
  assert.equal(phoneInput().value, "+44 20 5555 0199");
});

/* Resuming an application that predates the country column.
 *
 * Screen 1 asks where a physician is licensed, and emptyCredentials seeds that
 * field empty so `countrySet` in completeness.ts can tell "answered US" from
 * "never asked". Rows written before the column existed have neither: their
 * physician passed screen 1 under a bundle that could not ask, and was treated
 * as US the whole way through. Resuming them with a blank would put a red
 * marker on a question nobody ever put to them.
 */
const RESUMED = {
  status: "pending", product: "asclepius", step: 4,
  director_first_name: "Asha", director_last_name: "Rao",
  director_email: "asha@aiimsjodhpur.edu.in",
  director_password_set: true,
  director_cv: { uploaded: true, stage: "done", parsed: null },
};

async function resume(session) {
  global.fetch = async () => new Response(JSON.stringify(session), { status: 200 });
  await mount(Wizard, { token: "recovery-token" });
}

const countryValues = () =>
  [...document.querySelectorAll("select")]
    .filter((s) => [...s.options].some((o) => o.value === "ZW"))
    .map((s) => s.value);

test("an application older than the country column resumes as US", async () => {
  await resume(RESUMED);
  const values = countryValues();
  assert.ok(values.length >= 1, "the Review screen showed no country question");
  for (const v of values) {
    assert.equal(v, "US", "a legacy application resumed with its country forgotten");
  }
});

test("a stored country still wins over the legacy fallback", async () => {
  await resume({ ...RESUMED, director_country_of_licensure: "GB" });
  for (const v of countryValues()) {
    assert.equal(v, "GB", "the answer the physician gave on screen 1 was overwritten");
  }
});

test("an application that never reached screen one is not assumed American", async () => {
  // No password set means screen 1 is still ahead of them, so there is no
  // accepted answer to restore and the fallback must not invent one.
  await resume({ ...RESUMED, director_password_set: false, step: 0, director_cv: {} });
  assert.equal(document.querySelector('input[type="email"]').value,
               "asha@aiimsjodhpur.edu.in");
});
