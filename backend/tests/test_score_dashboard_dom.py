"""The contributor score is internal, and stays that way.

THE GUARANTEE IN THIS FILE REVERSED DIRECTION. It used to assert the rating was
ON the physician's dashboard (PRD-SCORE); it now asserts it reaches no
physician-facing surface at all. That is a product decision, not a weakened
test: the score is an instrument for routing and for pay, and showing it to the
person it measures turned it into a number they were managing rather than a
measurement of the work.

Nothing about how it is computed changed, and the admin still reads it. The
last two tests exist to keep that half honest, so "hidden from physicians"
cannot quietly become "deleted".

Source assertions, same convention as test_no_first_run_intro: there is no
DOM harness for the portal monolith, and the failure being guarded is
"somebody wired it back up".
"""

from __future__ import annotations

from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_PORTAL_JS = _FRONTEND / "asclepius.js"
_ADMIN_JS = _FRONTEND / "admin_physicians.js"
_CSS = _FRONTEND / "asclepius.css"


import re

_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")


def _src() -> str:
    return _PORTAL_JS.read_text(encoding="utf-8")


def _code(src: str) -> str:
    """Comments stripped. A comment that EXPLAINS the removal must not read as
    the removed thing still being there."""
    return _LINE_COMMENT.sub("", src)


def test_no_contributor_score_reaches_a_physician():
    """Every surface the old rating lived on, asserted gone."""
    src = _code(_src())
    for gone in ("function renderScoreWidget", "function openScoreInfo",
                 "api('/score')", "asc-score-widget", "asc-score-value"):
        assert gone not in src, gone


def test_the_portal_never_speaks_the_grading_vocabulary_to_a_physician():
    """The number was only half of it. The explainer modal and the LinkedIn
    helper disclosed the whole model: the 100-point scale, both band
    thresholds, and what a given field was worth. Removing the widget while
    leaving the prose would have hidden the score and kept the anxiety."""
    src = _code(_src())
    for phrase in ("out of 100", "100-point", "Reviewer threshold",
                   "Reviewer band", "worth 3 of those points",
                   "Contributor score"):
        assert phrase not in src, phrase


def test_the_self_profile_endpoint_stops_shipping_a_rating_it_cannot_render():
    """Dead payload on the one endpoint whose contract is "everything the portal
    shows a physician about their own account"."""
    router = (Path(__file__).resolve().parents[1]
              / "routers" / "asclepius.py").read_text(encoding="utf-8")
    standing = router[router.index('"standing": {'):]
    standing = standing[:standing.index("},")]
    assert '"score"' not in standing
    assert '"band"' not in standing
    # Capability vocabulary, not a rating. Other things read these.
    assert '"tier_word"' in standing


def test_the_case_difficulty_chip_is_no_longer_on_the_open_case_header():
    """It used to be, and deliberately is not any more.

    The Doctor Portal UX PRD §7 deleted the metadata chip bar above the clinical
    question: specialty, difficulty, modality and capture mode are our routing
    vocabulary, and telling a specialist "Difficulty: hard" before they read the
    chart primes the answer. The difficulty is still on the task record and
    still drives routing — it is only no longer shown to the physician grading
    the case. The full set of removal assertions lives in test_portal_ux.py.
    """
    src = _src()
    assert "DIFFICULTY_DOT" not in src
    assert "'Difficulty: ' + diff" not in src


def test_the_admin_profile_shows_the_score_and_trajectory():
    admin = _ADMIN_JS.read_text(encoding="utf-8")
    assert "/admin/scores/" in admin
    assert "Contributor score" in admin
    assert "Trajectory" in admin


def test_the_widget_styles_went_with_the_widget():
    """The repo fails a build when a styled class is emitted by nothing, so the
    rules have to leave in the same commit as their emitter. The admin's history
    row stays, because the admin surface stays."""
    css = _CSS.read_text(encoding="utf-8")
    for gone in (".asc-score-widget", ".asc-score-value", ".asc-score-band",
                 ".asc-score-review", ".asc-score-note", ".asc-score-more"):
        assert gone not in css, gone
    for kept in (".asc-dash-side", ".asc-score-hist-row"):
        assert kept in css, kept
