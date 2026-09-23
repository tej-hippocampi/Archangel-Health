/* Build the REAL onboarding components into something JSDOM can mount.
 *
 * The one rule this file exists to enforce: it copies production sources
 * verbatim. The investigation harness this grew out of
 * (docs/prd/onboarding-master/evidence/onboarding-ux-evidence) patched
 * `Group` to module scope in a disposable copy, to prove the root cause
 * without touching the product. That patch is gone. If a check below fails,
 * production is broken — there is no variant of the source that could be
 * passing instead.
 *
 * esbuild only strips types and resolves the two `@/lib` aliases. React,
 * react-dom and the JSX runtime stay external so the test and the components
 * share one React instance (two copies would break hooks).
 */
const fs = require("node:fs");
const path = require("node:path");

const DEPS = process.env.ARCHANGEL_UX_DEPS
  || path.join(__dirname, "node_modules");
const esbuild = require(path.join(DEPS, "esbuild"));

const repo = path.resolve(__dirname, "..", "..");
const src = path.join(repo, "landing/src/app/components/onboarding");
const out = path.join(process.env.ARCHANGEL_UX_OUT || path.join(__dirname, ".build"));

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

/* React, react-dom and the JSX runtime are left external so the bundle and the
   test share ONE React instance — two copies in one process throw on the first
   hook. External means the emitted file calls `require("react")`, which resolves
   from the bundle's own directory, so when the dependencies live somewhere else
   (a CI cache, a scratch install) the build links them into place rather than
   asking every caller to get NODE_PATH right. */
const localModules = path.join(out, "node_modules");
if (path.resolve(DEPS) !== path.resolve(localModules)) {
  try {
    fs.symlinkSync(path.resolve(DEPS), localModules, "dir");
  } catch (err) {
    if (err.code !== "EEXIST") throw err;
  }
}

for (const f of ["steps.tsx", "primitives.tsx", "completeness.ts"]) {
  fs.copyFileSync(path.join(src, f), path.join(out, f));
}
fs.copyFileSync(path.join(repo, "landing/src/lib/npi.ts"), path.join(out, "npi.ts"));

/* The network is not merely unused, it is unreachable: any component that
   reaches for the API in a test gets a thrown error rather than a hang. */
fs.writeFileSync(path.join(out, "api.ts"), `
export const API_BASE = '/network-disabled-in-tests';
export const apiHeaders = () => ({});
export const asclepiusPortalUrl = () => '';
export const redirectToAsclepiusPortal = () => {
  throw new Error('Navigation is disabled in the onboarding component tests');
};
`);

/* A host that owns the credentials state exactly as OnboardingWizard does, so
   the components under test see the real update path (a patch merged into
   parent state on every change) rather than a simplified stand-in. */
fs.writeFileSync(path.join(out, "entry.tsx"), `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Step5Credentials, emptyCredentials, withRowIds } from './steps';

export function mount(el, options = {}) {
  const controller = {};
  function Host() {
    const [data, setData] = useState({
      // withRowIds mirrors the real hydration path: OnboardingWizard applies it
      // to credentials arriving from the server, because a fixture (or a saved
      // blob) that spreads over the defaults replaces the row arrays and drops
      // the ids with them. Without it these tests would exercise a row shape
      // the product never actually renders.
      credentials: withRowIds({
        ...emptyCredentials('Mike Blum'),
        primarySpecialty: 'Nephrology',
        ...options.credentials,
      }),
      cvParsed: { ok: true },
      cvAutofilled: options.chips || [],
      ...options.data,
    });
    controller.data = data;
    return (
      <Step5Credentials
        data={data}
        setData={(patch) => setData((prev) => ({ ...prev, ...patch }))}
        onNext={async () => false}
        onBack={() => {}}
        eyebrow="COMPONENT TEST"
        reviewMode={options.reviewMode !== false}
        phase={options.phase}
      />
    );
  }
  const root = createRoot(el);
  root.render(<Host />);
  controller.unmount = () => root.unmount();
  return controller;
}
`);

/* Screen 1 owns the whole OnboardingData object and commits a country on
   Continue, so it needs its own host rather than a flag on the one above.
   `onNext` records the data as it stood when Continue was pressed, which is
   what the wizard posts. */
fs.writeFileSync(path.join(out, "entry-step1.tsx"), `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Step1NameEmail, emptyCredentials, withRowIds } from './steps';

export function mountStep1(el, options = {}) {
  const controller = { submitted: null };
  function Host() {
    const [data, setData] = useState({
      firstName: 'Eleanor',
      lastName: 'Whitfield',
      email: 'eleanor@nhs-trust.example',
      password: '',
      passwordSet: true,
      product: options.product || 'asclepius',
      attestations: {},
      credentials: withRowIds({
        ...emptyCredentials('Eleanor Whitfield'),
        ...options.credentials,
      }),
      ...options.data,
    });
    controller.data = data;
    return (
      <Step1NameEmail
        data={data}
        setData={(patch) => setData((prev) => ({ ...prev, ...patch }))}
        onNext={async () => { controller.submitted = controller.data; return false; }}
        kind={options.kind || 'physician'}
      />
    );
  }
  const root = createRoot(el);
  root.render(<Host />);
  controller.unmount = () => root.unmount();
  return controller;
}
`);

for (const [entry, outfile] of [["entry.tsx", "form.cjs"], ["entry-step1.tsx", "step1.cjs"]]) {
esbuild.buildSync({
  entryPoints: [path.join(out, entry)],
  bundle: true,
  platform: "node",
  format: "cjs",
  outfile: path.join(out, outfile),
  jsx: "automatic",
  external: ["react", "react-dom", "react-dom/client", "react/jsx-runtime"],
  alias: {
    "@/lib/auth-api": path.join(out, "api.ts"),
    "@/lib/npi": path.join(out, "npi.ts"),
    "@/lib/countries.json": path.join(repo, "landing/src/lib/countries.json"),
  },
  logLevel: "silent",
});
}

module.exports = {
  bundlePath: path.join(out, "form.cjs"),
  step1BundlePath: path.join(out, "step1.cjs"),
};

if (require.main === module) {
  console.log("built production onboarding components ->", path.join(out, "form.cjs"));
}
