"""Clinical validity is answered once, at the opening case review."""
from pathlib import Path
from tests._js_source import strip_js_comments
JS = strip_js_comments((Path(__file__).resolve().parents[2] / "frontend/asclepius/asclepius.js").read_text())


def test_validity_is_captured_at_the_initial_gate_and_not_repeated():
    assert "Looks clinically valid, continue" in JS
    start = JS.index("function validatePrompt()")
    end = JS.index("async function flagPrompt()", start)
    assert "attest_clinically_valid: true" in JS[start:end]
    assert "renderClinicalValidityCard" not in JS
    assert "I attest that this case is clinically valid" not in JS


def test_invalid_cases_can_still_be_flagged():
    assert "Flag as invalid" in JS
    assert "function flagPrompt()" in JS


def test_resumed_legacy_valid_review_is_accepted_but_explicit_false_is_not():
    assert "attest_clinically_valid === false" in JS
    assert "verdict !== 'valid'" in JS
    assert "attest_clinically_valid !== true" not in JS
