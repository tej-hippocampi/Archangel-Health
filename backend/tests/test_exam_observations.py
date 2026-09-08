"""The examination, made readable to the person deciding, without being judged.

Two things had to be true at once. The founders were explicit that no code
decides whether a physician is good enough, and ``_examination_block`` says so
in its own docstring. But the artifact the decision rests on was ALSO invisible:
the block was computed and served since the examination shipped and no screen
ever rendered it, so an admin was comparing free-text answers against an answer
key in their head, differently every time.

The reconciliation is the distinction these tests exist to hold. A VERDICT is
"this physician is good enough". A FACT is "they rejected candidate B, and B is
the one this case was authored to make wrong". The first is the admin's call.
The second is reading, and doing it in code is only automating the part that was
never a judgement.

So: no score, no total, no band, no pass, no fail, anywhere.
"""

from __future__ import annotations

import inspect
import pathlib
import re

from asclepius import exam_case, exam_grading

_ADMIN_JS = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
             / "admin_physicians.js").read_text(encoding="utf-8")

_CASE = {
    "intended_flawed_id": "B",
    "case": {"ground_truth": {
        "answer": "Low-solute hyponatremia, cap correction.",
        "rationale": "Low urine osm distinguishes it from SIADH.",
        "key_data": ["urine osmolality 120 (LOW)", "urine sodium 14", "recent thiazide"],
    }},
}


def _payload(**kw):
    base = {"rejected_id": "B", "chosen_id": "A",
            "independent_answer": {"text": ""}, "time_spent_sec": 600}
    base.update(kw)
    return base


# ── Facts, and only facts ───────────────────────────────────────────────────

def test_it_says_which_candidate_the_case_was_authored_to_make_wrong():
    obs = exam_grading.observations(_CASE, _payload(rejected_id="B"))
    assert obs["rejected_the_flawed_candidate"] is True
    assert obs["intended_flawed_id"] == "B"

    missed = exam_grading.observations(_CASE, _payload(rejected_id="A"))
    assert missed["rejected_the_flawed_candidate"] is False


def test_it_never_emits_a_score_a_band_or_a_verdict():
    """The whole reason this may sit beside a promise not to judge."""
    obs = exam_grading.observations(_CASE, _payload(
        independent_answer={"text": "urine osm 120, urine sodium 14, thiazide"}))
    for forbidden in ("score", "band", "passed", "failed", "grade", "tier",
                      "recommendation", "verdict_score"):
        assert forbidden not in obs, forbidden
    # A ratio is a score wearing a different hat. The matched and missed
    # phrases are handed back whole, for a person to read.
    assert isinstance(obs["key_data_matched"], list)
    assert isinstance(obs["key_data_missed"], list)


def test_the_module_computes_nothing_from_a_model():
    """Deterministic by construction. A judged examination that depended on an
    LLM would mean an applicant's decision moved when a model was swapped."""
    src = inspect.getsource(exam_grading)
    for forbidden in ("call_llm", "anthropic", "openai", "import ai"):
        assert forbidden not in src, forbidden


def test_a_case_with_no_answer_key_is_ungraded_rather_than_a_miss():
    """An empty checklist reads as a physician having missed everything, which
    is a claim about them we cannot make from a retired case."""
    obs = exam_grading.observations({"case": {}}, _payload())
    assert obs["graded"] is False
    assert obs.get("key_data_missed") is None


# ── The phrase matching, which is weak on purpose ───────────────────────────

def test_it_reads_the_key_through_how_a_physician_actually_writes():
    """"urine osm was 120" and "urine osmolality 120 (LOW)" are the same
    observation. A whole-phrase match would call the second one missing."""
    obs = exam_grading.observations(_CASE, _payload(
        independent_answer={"text": "The urine osm was 120 and urine sodium 14."}))
    assert "urine osmolality 120 (LOW)" in obs["key_data_matched"]
    assert "urine sodium 14" in obs["key_data_matched"]
    assert "recent thiazide" in obs["key_data_missed"]


def test_it_reads_every_place_the_physician_wrote_prose():
    """A doctor who names the deciding lab in a rubric row rather than in the
    free text has still named it."""
    obs = exam_grading.observations(_CASE, _payload(
        independent_answer={"text": ""},
        rubric=[{"text": "must note the thiazide", "points": 3}],
        reasoning_steps=[{"text": "urine sodium 14 points away from SIADH"}]))
    assert "recent thiazide" in obs["key_data_matched"]
    assert "urine sodium 14" in obs["key_data_matched"]


def test_an_empty_answer_matches_nothing():
    obs = exam_grading.observations(_CASE, _payload())
    assert obs["key_data_matched"] == []
    assert len(obs["key_data_missed"]) == 3


# ── Whose specialty was it ──────────────────────────────────────────────────

def test_an_unrecorded_own_specialty_flag_is_unknown_and_not_no():
    """True of every attempt filed before the column existed. Rendering it as
    "no" would invent a fact about the physician."""
    assert exam_grading.own_specialty({}) is None
    assert exam_grading.own_specialty({"is_own_specialty": 0}) is False
    assert exam_grading.own_specialty({"is_own_specialty": 1}) is True


def test_a_physician_who_types_their_specialty_the_way_physicians_do_gets_their_own_case():
    """THE BUG. exam_specialty tested raw lowercase against the case-set keys,
    so every one of these fell past it to the nephrology fallback: a
    cardiologist was handed a nephrology case and told, correctly and
    uselessly, that we had no set for their specialty."""
    for typed in ("interventional cardiology", "Pediatric Cardiology",
                  "cardiologist", "Cardiology"):
        picked = exam_case.exam_specialty({"specialty": typed})
        assert picked["specialty"] == "cardiology", typed
        assert picked["is_own"] is True, typed


def test_a_specialty_we_hold_no_cases_for_still_falls_back_and_says_so():
    """Hepatology is enabled for generation and has no gold set. Matching it
    and then failing to draw would 404 an applicant mid-examination."""
    picked = exam_case.exam_specialty({"specialty": "hepatology"})
    assert picked["specialty"] == exam_case.FALLBACK_SPECIALTY
    assert picked["is_own"] is False


def test_an_unrelated_specialty_is_never_guessed_into_one_we_hold():
    """match_specialty returns None rather than guessing, and a WRONG specialty
    is worse than a missing one."""
    picked = exam_case.exam_specialty({"specialty": "orthopaedic surgery"})
    assert picked["is_own"] is False


# ── The screen that makes any of it worth computing ─────────────────────────

def test_the_decision_screen_renders_the_examination():
    """It was computed and served and never drawn. The optional practice case
    had a card on the row and the dossier; the piece we actually read had
    neither."""
    assert "function examinationCard" in _ADMIN_JS
    assert "examinationCard(ctx, d.examination)" in _ADMIN_JS
    assert "examCell(h, r.examination)" in _ADMIN_JS


def test_the_examination_sits_above_the_practice_case():
    """One is a guided tour with a skip button on every screen. The other is
    the one we read. Order on the page is an argument about which."""
    assert (_ADMIN_JS.index("examinationCard(ctx, d.examination)")
            < _ADMIN_JS.index("practiceCaseCard(ctx, d.practice_case"))


def test_the_card_prints_no_verdict_words():
    start = _ADMIN_JS.index("function examinationCard")
    card = _ADMIN_JS[start:_ADMIN_JS.index("\n  function practiceCaseCard")]
    for forbidden in ("Passed", "Failed", "Score", "out of", "%"):
        assert forbidden not in card, forbidden
    assert "Facts, not a verdict" in card


def test_the_abbreviation_rule_does_not_become_a_coincidence_generator():
    """Prefix matching earns its keep on "osm" for osmolality, and would lose
    it instantly if two-letter fragments counted: "na" is a prefix of "nausea"
    and a key that turned on the sodium would be satisfied by a symptom."""
    case = {"intended_flawed_id": "B",
            "case": {"ground_truth": {"key_data": ["na 110"]}}}
    obs = exam_grading.observations(case, _payload(
        independent_answer={"text": "The patient reports nausea and vomiting."}))
    assert obs["key_data_matched"] == []


def test_the_flawed_candidate_is_found_on_a_stored_task_row():
    """gold_cases.py carries intended_flawed_id at the top level of the case
    entry; a TASK ROW carries it under `generation` with the rest of the
    provenance. Reading only the top level found nothing, and the candidate
    observation vanished from the decision screen while the key-data half of
    the same card kept working. Caught in a browser, not by a test, which is
    why there is one now."""
    stored = {"generation": {"mode": "gold_seed", "intended_flawed_id": "B"},
              "case": {"ground_truth": {"key_data": ["urine sodium 14"]}}}
    obs = exam_grading.observations(stored, _payload(rejected_id="B"))
    assert obs["intended_flawed_id"] == "B"
    assert obs["rejected_the_flawed_candidate"] is True
