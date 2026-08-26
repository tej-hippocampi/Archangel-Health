"""PRD-SCORE — the rating is on the dashboard, and honest about its state.

Source assertions, same convention as test_no_first_run_intro: there is no
DOM harness for the portal monolith, and the failure being guarded is
"nobody wired it up".
"""

from __future__ import annotations

from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_PORTAL_JS = _FRONTEND / "asclepius.js"
_ADMIN_JS = _FRONTEND / "admin_physicians.js"
_CSS = _FRONTEND / "asclepius.css"


def _src() -> str:
    return _PORTAL_JS.read_text(encoding="utf-8")


def test_the_dashboard_fetches_and_renders_the_score():
    src = _src()
    assert "api('/score')" in src
    assert "function renderScoreWidget" in src
    assert "renderScoreWidget(scoreInfo)" in src


def test_the_in_review_state_says_so_in_the_agreed_words():
    assert "Your profile is currently in review." in _src()


def test_the_more_info_popover_explains_bands_and_updates():
    src = _src()
    assert "function openScoreInfo" in src
    assert "updates after every completed, QA-graded case" in src
    assert "review other physicians" in src
    assert "Everyone can label" in src


def test_the_score_never_renders_from_a_missing_payload():
    """Absent payload = absent widget, never a reassuring zero."""
    assert "if (!info || info.score == null) return null;" in _src()


def test_the_case_difficulty_chip_is_on_the_open_case_header():
    """The workspace meta row carries the colored difficulty chip."""
    src = _src()
    assert "DIFFICULTY_DOT" in src
    assert "'Difficulty: ' + diff" in src


def test_the_admin_profile_shows_the_score_and_trajectory():
    admin = _ADMIN_JS.read_text(encoding="utf-8")
    assert "/admin/scores/" in admin
    assert "Contributor score" in admin
    assert "Trajectory" in admin


def test_the_widget_styles_exist():
    css = _CSS.read_text(encoding="utf-8")
    for cls in (".asc-score-widget", ".asc-score-value", ".asc-score-review",
                ".asc-dash-side", ".asc-score-hist-row"):
        assert cls in css, cls
