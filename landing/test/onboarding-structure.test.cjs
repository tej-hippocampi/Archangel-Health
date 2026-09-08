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

/* DISCOVERED, not listed. The invariant says "anywhere in `onboarding/`", and a
   hardcoded list means the next file added to that directory is silently
   unchecked — which is the same failure mode as a test file that falls out of a
   CI shard. The wizard is named explicitly because it lives one level up and
   owns the same components. Only the app's own sources are scanned: the frozen
   evidence copies under docs/ deliberately preserve the pre-fix shape. */
const ONBOARDING_DIR = path.join(repo, "landing/src/app/components/onboarding");
const FILES = [
  ...fs.readdirSync(ONBOARDING_DIR)
    .filter((f) => /\.tsx?$/.test(f))
    .map((f) => path.posix.join("landing/src/app/components/onboarding", f)),
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
/* A signature wrapped over several lines. `const Foo = (` alone is NOT enough —
   `const Tag = (htmlFor ? "label" : "div")` is a tag name, not a component — so
   the arrow has to actually turn up, within a few lines and before the
   statement ends. */
const NESTED_ARROW_OPEN = /^[ \t]+const ([A-Z][A-Za-z0-9]*) *(?::[^=]*)?= *\($/;
const NESTED_FUNCTION = /^[ \t]+function ([A-Z][A-Za-z0-9]*) *[(<]/;
const SCREAMING = /^[A-Z0-9_]+$/;
const ARROW_WITHIN = 12;

function nestedComponents(relPath) {
  const lines = fs.readFileSync(path.join(repo, relPath), "utf8").split("\n");
  const found = [];
  lines.forEach((line, i) => {
    let m = NESTED_ARROW.exec(line) || NESTED_FUNCTION.exec(line);
    if (!m) {
      const open = NESTED_ARROW_OPEN.exec(line);
      if (open) {
        for (let j = i + 1; j < Math.min(lines.length, i + ARROW_WITHIN); j++) {
          if (/=>/.test(lines[j])) { m = open; break; }
          if (/;\s*$/.test(lines[j])) break;
        }
      }
    }
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
    "  const Group = ({ n, children }: { n: 0 | 1 | 2; children: ReactNode }) => {",  // 1
    "    if (!reviewMode) return <>{children}</>;",
    "  };",
    "  function Inner({ x }: { x: number }) { return <b>{x}</b>; }",                  // 2
    "  const Wrapped = (",                                                            // 3
    "    { x }: { x: number },",
    "  ) => <b>{x}</b>;",
    // And the shapes that must NOT match:
    "  const THREE_COL = { display: 'grid' };",
    "  const notAComponent = ({ a }) => a + 1;",
    "  const Tag = (htmlFor ? 'label' : 'div') as 'label' | 'div';",
    "function ModuleScopeIsFine({ x }) { return <b>{x}</b>; }",
  ];
  const tmp = path.join(require("node:os").tmpdir(),
                        "onb-structure-selftest-" + process.pid + ".tsx");
  fs.writeFileSync(tmp, before.join("\n"));
  try {
    /* Run the REAL detector over a real file, rather than re-implementing the
       matching here — a self-test that duplicates the logic tests the copy. */
    const rel = path.relative(repo, tmp);
    const hits = nestedComponents(rel);
    assert.equal(hits.length, 3,
                 "expected exactly the three nested components, got:\n" + hits.join("\n"));
    assert.ok(hits.some((h) => /\bGroup\b/.test(h)));
    assert.ok(hits.some((h) => /\bInner\b/.test(h)));
    assert.ok(hits.some((h) => /\bWrapped\b/.test(h)), "a wrapped signature escaped");
    assert.ok(!hits.some((h) => /\bTag\b/.test(h)), "a tag-name const was flagged");
  } finally {
    fs.rmSync(tmp, { force: true });
  }
});
