"""§3 A/B divergence-diff regression (Evaluation UX Overhaul PRD).

The compare-stage divergence diff is computed client-side (asclepius.js). The
historical bug: ``computeAnswerDiff`` returned ``null`` when the two answers
shared *no* normalized sentence — exactly the genuinely-hard cases — so the
"Highlight differences" view silently rendered plain text. §3.1 fixes that with
an all-divergent fallback + Jaccard soft matching, both scoped to V3/V4.

Like the rubric UI-parity approach, this test extracts the PURE functions from
``asclepius.js`` verbatim (no app state, no DOM) and executes them under node,
so the shipped frontend logic is what's asserted — not a Python re-derivation.
Skipped when node isn't installed.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

JS_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "asclepius"
    / "asclepius.js"
)

# The pure sentence-diff pipeline, in dependency order.
_FUNCS = (
    "splitSentences",
    "normSentence",
    "sentTokenSet",
    "tokenJaccard",
    "diffFlags",
    "buildAnswerDiff",
)


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {name} from asclepius.js")


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def diff_env() -> str:
    src = JS_PATH.read_text(encoding="utf-8")
    return "\n".join(_extract_function(src, f) for f in _FUNCS)


# Two answers that share no sentence at all — the cardiorenal-style hard case.
_FULLY_DIVERGENT_A = (
    "Continue IV diuresis despite the creatinine rise. "
    "The patient remains congested with JVP at 12. "
    "Add metolazone if urine output lags. "
    "Recheck a basic metabolic panel in the morning."
)
_FULLY_DIVERGENT_B = (
    "Hold decongestion and reassess volume status first, because the rising "
    "creatinine most likely reflects intravascular underfilling rather than "
    "true progression of kidney disease, and forcing further diuresis now "
    "risks tipping the patient into frank prerenal azotemia."
)


def test_fully_divergent_answers_still_highlight(diff_env: str) -> None:
    """§3 acceptance: on a case where A and B share no sentences, the V3/V4 diff
    is NON-NULL, flags every sentence divergent, and says so (allDivergent)."""
    script = (
        diff_env
        + f"""
const A = {json.dumps(_FULLY_DIVERGENT_A)};
const B = {json.dumps(_FULLY_DIVERGENT_B)};
const v3 = buildAnswerDiff(A, B, {{ soft: true, markAllWhenDisjoint: true }});
const legacy = buildAnswerDiff(A, B, {{}});
console.log(JSON.stringify({{
  v3NonNull: v3 !== null,
  allDivergent: v3 && v3.allDivergent,
  aAllChanged: v3 && v3.A.shared.every((s) => s === false),
  bAllChanged: v3 && v3.B.shared.every((s) => s === false),
  aSentCount: v3 ? v3.A.sents.length : 0,
  aRoundTrips: v3 ? v3.A.sents.join('') === A : false,
  bRoundTrips: v3 ? v3.B.sents.join('') === B : false,
  legacyIsNull: legacy === null,
}}));
"""
    )
    out = _run_node(script)
    assert out["v3NonNull"], "fully-divergent answers must still produce a diff (the §3 bug)"
    assert out["allDivergent"]
    assert out["aAllChanged"] and out["bAllChanged"]
    assert out["aSentCount"] >= 2
    # Character-exact split: rendering the diff never alters the answer text.
    assert out["aRoundTrips"] and out["bRoundTrips"]
    # V1/V2 keep the legacy behavior (null → plain text) byte-for-byte.
    assert out["legacyIsNull"]


def test_partially_shared_answers_dim_shared_sentences(diff_env: str) -> None:
    script = (
        diff_env
        + """
const shared = "Check the potassium before dosing. ";
const A = shared + "Start furosemide 40 mg IV twice daily.";
const B = shared + "Start a thiazide and recheck sodium tomorrow.";
const d = buildAnswerDiff(A, B, { soft: true, markAllWhenDisjoint: true });
console.log(JSON.stringify({
  nonNull: d !== null,
  allDivergent: d.allDivergent,
  aFirstShared: d.A.shared[0],
  bFirstShared: d.B.shared[0],
  aLastShared: d.A.shared[d.A.shared.length - 1],
  bLastShared: d.B.shared[d.B.shared.length - 1],
}));
"""
    )
    out = _run_node(script)
    assert out["nonNull"]
    assert out["allDivergent"] is False
    assert out["aFirstShared"] and out["bFirstShared"], "shared boilerplate must dim"
    assert not out["aLastShared"] and not out["bLastShared"], "divergent tails must brighten"


def test_soft_matching_counts_near_identical_sentences_as_shared(diff_env: str) -> None:
    """§3.1: token-set Jaccard ≥ 0.85 counts as shared under soft matching (V3/V4)
    while exact-only matching (V1/V2) still treats the pair as divergent."""
    script = (
        diff_env
        + """
// 14 of 15 normalized tokens shared -> Jaccard ~0.93 (>= 0.85); one token differs.
const A = "Continue the reduced dose of metformin and recheck the egfr in three months watching closely for lactic acidosis signs.";
const B = "Continue the reduced dose of metformin and recheck the egfr in three months watching carefully for lactic acidosis signs.";
const soft = diffFlags(splitSentences(A), splitSentences(B), true);
const exact = diffFlags(splitSentences(A), splitSentences(B), false);
console.log(JSON.stringify({ softShared: soft.a[0], exactShared: exact.a[0] }));
"""
    )
    out = _run_node(script)
    assert out["softShared"], "near-identical clinical sentences must count as shared under soft matching"
    assert not out["exactShared"], "V1/V2 exact matching must be unchanged"


def test_admin_task_list_carries_ab_meta_fields() -> None:
    """§4.2: the admin /tasks handler decorates baseline tasks with ab_meta
    (provider + prompt_hash per slot) and surfaces needs_baseline. Static check
    that the decoration exists in the router (behavioral coverage lives in
    test_asclepius_two_frontier.py's pair-assembly tests)."""
    router_src = (
        pathlib.Path(__file__).resolve().parents[1] / "routers" / "asclepius.py"
    ).read_text(encoding="utf-8")
    assert '"ab_meta"' in router_src
    assert '"prompt_hash_match"' in router_src
    assert '"two_providers"' in router_src
