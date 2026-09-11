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
    export {default as Reset} from './app/components/ResetPasswordPage';`, resolveDir: src },
  bundle: true, platform: "node", format: "cjs", jsx: "automatic",
  outfile: path.join(out, "components.cjs"),
  external: ["react", "react-dom", "react-dom/client", "react/jsx-runtime"],
  alias: { "@/lib/auth-api": path.join(src, "lib/auth-api.ts"), "@/lib/npi": path.join(src, "lib/npi.ts") },
  define: { "import.meta.env": '{"DEV":true}' }, logLevel: "silent",
});
const { Wizard, Reset } = require(path.join(out, "components.cjs"));
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
const specialtyInput = () => document.querySelector('input[placeholder="Nephrology"]');
const phoneInput = () => document.querySelector('input[type="tel"]');

test("resuming a parsed CV restores unsaved suggestions and retains physician edits", async () => {
  global.fetch = async (url) => new Response(JSON.stringify(url.includes("/session?")
    ? cvSession("done", { phone: "+44 20 5555 0101", cvManualFields: ["phone"] })
    : { countries: [] }));
  await mount(Wizard, { token: "recovery-token" });
  assert.equal(specialtyInput().value, "Nephrology");
  assert.equal(phoneInput().value, "+44 20 5555 0101");
});

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
