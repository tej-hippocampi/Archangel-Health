"""PRD M — the instruction manuals, and the rules that keep them readable.

The existing manual holds its three-line rule by discipline. Discipline does not
survive three authors and a year, so the rule is a test now.

Everything here is static: the manuals are structured data, so they are read out
of ``manual-content.js`` with node and asserted in Python. No browser, no server,
no fixtures — the whole file runs in under a second, which is what makes it a
check people actually keep.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
_MANUAL_JS = _FRONTEND / "manual-content.js"
_APP_JS = _FRONTEND / "asclepius.js"
_INDEX_HTML = _FRONTEND / "index.html"

# The states ``users.verification_status`` may hold. NULL is "pre-verification-era
# account" and is deliberately not a `showWhen` target — a section shown to
# everyone who predates credential review is a section shown for no reason.
_VERIFICATION_STATES = {"pending", "approved", "rejected"}

_ROLES = ("labeler", "reviewer")


def _load() -> dict:
    """The manuals, as data. Executes the file the browser executes."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    script = (
        "global.window = {};"
        f"require({json.dumps(str(_MANUAL_JS))});"
        "console.log(JSON.stringify({"
        "  manuals: window.ASC_MANUALS,"
        "  aliasMatchesLabeler: window.ASC_MANUAL === window.ASC_MANUALS.labeler,"
        "}));"
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def data() -> dict:
    return _load()


@pytest.fixture(scope="module")
def manuals(data) -> dict:
    return data["manuals"]


def _words(text: str) -> int:
    return len((text or "").strip().split())


def _visible_words(manual: dict) -> int:
    """Everything a physician reads without expanding anything — which is the
    number the reading-time estimate has to be honest about. ``detail`` is
    excluded because it is collapsed by default and is where depth is supposed
    to go."""
    total = 0
    for sec in manual["sections"]:
        for field in ("what", "why", "how"):
            total += _words(sec.get(field))
        for callout in sec.get("callouts") or []:
            total += _words(callout.get("text"))
        example = sec.get("example") or {}
        total += _words(example.get("good")) + _words(example.get("weak"))
        for item in sec.get("list") or []:
            total += _words(item)
        total += _words(sec.get("note"))
    return total


# ─── §6.1 — the shape ─────────────────────────────────────────────────────────
def test_there_are_exactly_two_manuals(manuals):
    assert set(manuals) == set(_ROLES)


def test_the_old_global_still_points_at_the_labeler_manual(data):
    """Kept so nothing that referenced ``ASC_MANUAL`` breaks mid-merge — three
    other agents are in this tree and none of them should have to care that this
    file changed shape."""
    assert data["aliasMatchesLabeler"] is True


def test_every_manual_has_a_title_subtitle_and_reading_time(manuals):
    for role, manual in manuals.items():
        assert manual.get("title"), role
        assert manual.get("subtitle"), role
        assert isinstance(manual.get("readingTimeMin"), int), role
        assert manual["sections"], role


# ─── §6.2 — the rule that decays first ────────────────────────────────────────
def test_the_three_line_rule_holds_across_all_three_manuals(manuals):
    """Every section is exactly ``what`` / ``why`` / ``how``, one line each, at
    most twenty words. Depth belongs in ``detail``.

    A section is allowed to be STRUCTURAL instead — a checklist or a note, like
    the labeler manual's last two — but it may not be half of each. A section
    with a ``what`` and no ``why`` is how the rule dies: nobody notices, and the
    next author copies it.
    """
    failures = []
    for role, manual in manuals.items():
        for sec in manual["sections"]:
            three = [f for f in ("what", "why", "how") if sec.get(f)]
            structural = bool(sec.get("list") or sec.get("note"))
            if not three and structural:
                continue
            if len(three) != 3:
                failures.append(f"{role}/{sec['id']}: has {three or 'none'}, needs all three")
                continue
            for field in ("what", "why", "how"):
                n = _words(sec[field])
                if n > 20:
                    failures.append(f"{role}/{sec['id']}.{field}: {n} words (max 20)")
                if "\n" in sec[field]:
                    failures.append(f"{role}/{sec['id']}.{field}: is not one line")
    assert not failures, "three-line rule broken:\n  " + "\n  ".join(failures)


def test_depth_lives_in_detail_not_in_the_three_lines(manuals):
    """The counterpart to the rule above: if every section were three lines and
    nothing else, the manual would be a poster. ``detail`` is where the argument
    goes, and the sections that most need one have one."""
    for role, manual in manuals.items():
        with_detail = [s for s in manual["sections"] if s.get("detail")]
        assert len(with_detail) >= len(manual["sections"]) // 2, role
        for sec in with_detail:
            assert all(isinstance(d, str) and d.strip() for d in sec["detail"]), \
                f"{role}/{sec['id']}"


# ─── §6.3 — the word budget ───────────────────────────────────────────────────
def test_every_manual_is_under_the_visible_word_budget(manuals):
    for role, manual in manuals.items():
        n = _visible_words(manual)
        assert n <= 1000, f"{role} manual is {n} visible words (budget 1000)"


def test_the_reviewer_manual_is_shorter_than_the_labeler_manual(manuals):
    """A reviewer is senior and time-poor. If their manual reads longer than the
    labeling one, it is the wrong document."""
    assert _visible_words(manuals["reviewer"]) < _visible_words(manuals["labeler"])


# ─── §6.4 — identity ──────────────────────────────────────────────────────────
def test_ids_are_unique_within_each_manual(manuals):
    for role, manual in manuals.items():
        ids = [s["id"] for s in manual["sections"]]
        assert len(ids) == len(set(ids)), f"{role}: duplicate section id"
        for sid in ids:
            assert re.fullmatch(r"[a-z0-9-]+", sid), f"{role}/{sid}: id is an anchor, keep it a slug"


def test_section_numbers_are_unique_or_the_placeholder(manuals):
    """``num`` renders in the table of contents and in the anchor chrome, so two
    sections sharing one is a navigation bug. ``—`` is the deliberate exception:
    a section outside the numbered sequence."""
    for role, manual in manuals.items():
        nums = [s.get("num") for s in manual["sections"] if s.get("num") and s["num"] != "—"]
        assert len(nums) == len(set(nums)), f"{role}: duplicate section num"


# ─── §6.5 — the sentence this whole round exists to ship ──────────────────────
def test_the_reviewer_manual_states_the_unpaid_rule_in_those_words(manuals):
    """Pinned deliberately. This is the sentence the audit exists to get in front
    of a physician, and it is the difference between a doctor knowing the rule and
    a doctor discovering it by losing $100."""
    blob = json.dumps(manuals["reviewer"])
    assert "ends the session unpaid" in blob


def test_the_session_clock_section_carries_the_whole_rule(manuals):
    section = next(s for s in manuals["reviewer"]["sections"] if s["id"] == "the-session")
    assert section["num"] == "01"
    assert "$100" in section["what"]
    assert "twenty continuous minutes" in section["what"]
    assert "ends the session unpaid" in section["how"]

    warn = [c for c in section["callouts"] if c["kind"] == "warn"]
    assert len(warn) == 1
    assert "not a partial amount" in warn[0]["text"]

    # The line that makes the cliff defensible: the physician is told the records
    # exist and that they can contest a measurement.
    detail = " ".join(section["detail"])
    assert "Every session is recorded" in detail
    assert "tell us and we will look at the record" in detail


def test_the_payment_rules_are_stated_once(manuals):
    """A number that appears twice will disagree with itself. Section 09 points
    at section 01 rather than restating the threshold."""
    pay = next(s for s in manuals["reviewer"]["sections"] if s["id"] == "pay")
    visible = " ".join(filter(None, [pay.get("what"), pay.get("why"), pay.get("how")]))
    assert "twenty" not in visible.lower()
    assert "20" not in visible
    assert "section 01" in visible.lower()


# ─── §6.6 — colour semantics ──────────────────────────────────────────────────
def test_warn_is_used_at_most_once_per_manual(manuals):
    """``warn`` is pink, pink means something is lost, and a manual is not an
    alert surface. One per document, or the reader stops seeing it."""
    for role, manual in manuals.items():
        warns = [c for s in manual["sections"] for c in (s.get("callouts") or [])
                 if c.get("kind") == "warn"]
        assert len(warns) <= 1, f"{role} manual has {len(warns)} warn callouts"


def test_the_reviewer_warn_is_on_the_session_clock(manuals):
    """The only place in the reviewer's job where money is lost."""
    owner = [s["id"] for s in manuals["reviewer"]["sections"]
             if any(c.get("kind") == "warn" for c in (s.get("callouts") or []))]
    assert owner == ["the-session"]


def test_callout_kinds_are_from_the_existing_vocabulary(manuals):
    known = {"why", "warn", "mistake"}
    for role, manual in manuals.items():
        for sec in manual["sections"]:
            for callout in sec.get("callouts") or []:
                assert callout.get("kind") in known, f"{role}/{sec['id']}: {callout.get('kind')}"
                assert callout.get("text", "").strip()


# ─── §6.7 — the one conditional field ─────────────────────────────────────────
def test_every_show_when_names_a_real_verification_status(manuals):
    for role, manual in manuals.items():
        for sec in manual["sections"]:
            if "showWhen" in sec:
                assert sec["showWhen"] in _VERIFICATION_STATES, f"{role}/{sec['id']}"


def test_show_when_is_used_exactly_once_in_the_whole_design(manuals):
    """It exists for the waiting state and nothing else. A second use means
    somebody is gating a document instead of scoping one — which is the failure
    this PRD replaced."""
    conditional = [(role, s["id"]) for role, manual in manuals.items()
                   for s in manual["sections"] if "showWhen" in s]
    assert conditional == [("labeler", "awaiting-verification")]


def test_the_waiting_section_answers_what_is_happening_and_when(manuals):
    sec = next(s for s in manuals["labeler"]["sections"] if s["id"] == "awaiting-verification")
    assert sec["showWhen"] == "pending"
    assert sec["num"] == "—"
    body = " ".join([sec["what"], sec["why"], sec["how"]]).lower()
    assert "npi" in body                       # what is happening
    assert "business days" in body             # and when
    assert "email" in body                     # and how they find out


# ─── §6.8 — the renderer ──────────────────────────────────────────────────────
def _app_code() -> str:
    """asclepius.js with comments stripped, so the greps read code and not the
    prose explaining what the code refuses to do."""
    source = _APP_JS.read_text(encoding="utf-8")
    out, i, n = [], 0, len(source)
    while i < n:
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def _render_guide_body() -> str:
    code = _app_code()
    start = code.index("function renderGuide()")
    end = code.index("function guideSection(", start)
    return code[start:end]


def test_render_guide_introduces_no_innerhtml():
    body = _render_guide_body()
    assert "innerHTML" not in body
    assert "insertAdjacentHTML" not in body
    assert "document.write" not in body


def test_render_guide_picks_by_capability_never_by_tier():
    """An advisor labels and reviews and signs off, so a tier does not map to one
    manual. Reading ``users.tier`` here would reintroduce the exact two-state
    check ``capabilities.py`` exists to have removed."""
    body = _render_guide_body()
    assert "sessionCan" in body or "heldManuals" in body
    for banned in ("tier ===", "tier ==", ".tier)"):
        assert banned not in body, f"renderGuide reads the tier directly: {banned}"


def test_render_guide_filters_on_show_when():
    body = _render_guide_body()
    assert "showWhen" in body
    assert "verification_status" in body


def test_the_manual_script_still_loads_before_the_app():
    """``manual-content.js`` defines the globals ``renderGuide`` reads at render
    time; both are deferred, so document order is what guarantees it."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r'src="/static/asclepius/([\w.-]+\.js)"', html)
    assert "manual-content.js" in scripts
    assert scripts.index("manual-content.js") < scripts.index("asclepius.js")


# ─── Voice ────────────────────────────────────────────────────────────────────
def test_no_exclamation_marks_anywhere(manuals):
    """Professional instruction for busy specialists. The register is the
    existing manual's and it does not shout."""
    for role, manual in manuals.items():
        assert "!" not in json.dumps(manual), f"{role} manual raises its voice"


def test_examples_appear_only_where_a_judgment_is_subjective(manuals):
    """An example on an objective instruction is filler, and filler is what pushes
    a four-minute document past the point a senior physician finishes it."""
    reviewer = [s["id"] for s in manuals["reviewer"]["sections"] if s.get("example")]
    assert reviewer == ["verdict", "corrections"]
