/* The structural rule, enforced rather than remembered.
 *
 * Onboarding Master PRD §2 invariant 7: "no inline component definitions inside
 * render functions anywhere in onboarding/ (add a lint rule)".
 *
 * This is that rule. It is a test rather than an ESLint plugin because the
 * landing app has no ESLint pipeline and adding one to enforce a single
 * invariant would be a larger, less certain change than the invariant is worth —
 * and because a rule that runs in the same job as the behaviour tests cannot be
 * skipped separately from them.
 *
 * WHY THE RULE EXISTS. React reconciles by component TYPE. A component defined
 * in a render body is a new type on every render, so React does not update the
 * subtree — it unmounts and remounts it. In this form that meant every keystroke
 * replaced the input being typed into, reset every accordion, and discarded
 * every unfinished draft below it. The behaviour tests in
 * onboarding-form-stability.test.cjs prove the current tree is sound; this file
 * stops the next one being reintroduced, which a behaviour test can only do for
 * the specific interactions somebody thought to write down.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repo = path.resolve(__dirname, "..", "..");
const FILES = [
  "landing/src/app/components/onboarding/steps.tsx",
  "landing/src/app/components/onboarding/primitives.tsx",
  "landing/src/app/components/onboarding/completeness.ts",
  "landing/src/app/components/onboarding/OnboardingStyles.tsx",
  "landing/src/app/components/OnboardingWizard.tsx",
];

/* A component definition is a capitalised binding whose value is a function.
 * Indented means nested inside something — at module scope these start at
 * column 0. Both spellings are caught:
 *
 *     const Group = ({...}) => ...        an arrow component
 *     function Group({...}) {...}         a nested declaration
 *
 * A capitalised const holding a plain value (`const THREE_COL = {...}`) is not
 * matched: the pattern requires a function head. Screaming-snake constants are
 * excluded outright, since `const CV_STAGE_INDEX = (x) => ...` is a lookup
 * helper, not a component.
 */
const NESTED_ARROW = /^[ \t]+const ([A-Z][A-Za-z0-9]*) *(?::[^=]*)?= *(?:\([^)]*\)|[A-Za-z0-9_$]+) *(?::[^=]*)?=>/;
const NESTED_FUNCTION = /^[ \t]+function ([A-Z][A-Za-z0-9]*) *[(<]/;
const SCREAMING = /^[A-Z0-9_]+$/;

function nestedComponents(relPath) {
  const lines = fs.readFileSync(path.join(repo, relPath), "utf8").split("\n");
  const found = [];
  lines.forEach((line, i) => {
    const m = NESTED_ARROW.exec(line) || NESTED_FUNCTION.exec(line);
    if (!m || SCREAMING.test(m[1])) return;
    found.push(`${relPath}:${i + 1}  ${m[1]}  —  ${line.trim().slice(0, 90)}`);
  });
  return found;
}

for (const relPath of FILES) {
  test(`no component is defined inside a render function in ${path.basename(relPath)}`,
    () => {
      assert.deepEqual(
        nestedComponents(relPath), [],
        "A capitalised function defined at an indent is a component minted fresh on " +
        "every render. Move it to module scope and pass what it needs as ordinary " +
        "props — props may be recreated freely, component types may not. See the " +
        "note above ReviewGroup in steps.tsx.");
    });
}

test("the rule can actually see a violation", () => {
  /* A guard that matches nothing passes forever. This pins the detector against
     the exact shape that caused the defect — the real `Group` as it was
     written, verbatim from the commit before the hoist. */
  const before = [
    "  const Group = ({ n, children }: { n: 0 | 1 | 2; children: ReactNode }) => {",
    "    if (!reviewMode) return <>{children}</>;",
    "  };",
    "  function Inner({ x }: { x: number }) { return <b>{x}</b>; }",
    "  const THREE_COL = { display: 'grid' };",
    "  const notAComponent = ({ a }) => a + 1;",
    "function ModuleScopeIsFine({ x }) { return <b>{x}</b>; }",
  ];
  const hits = before.filter(
    (line) => {
      const m = NESTED_ARROW.exec(line) || NESTED_FUNCTION.exec(line);
      return m && !SCREAMING.test(m[1]);
    });
  assert.deepEqual(hits.length, 2, "expected exactly the two nested components");
});
