"""The review screen, and what it is allowed to imply about a physician.

Step 4 renders every credential field at once: roughly thirty controls on one
scroll. The complaint was not the length. It was that nothing on the page
distinguished a field the CV filled in from a field the physician typed, or
either of those from the two that actually stop them submitting, or any of them
from the twenty six that do not. All four looked identical, so the honest
reading of the page was "everything here is required and half of it is somehow
already done".

Two properties matter more than the layout and both are asserted here.

RED IS SCARCE AND RED NEVER GATES. A pink marker on eight fields is a page of
noise, and a pink marker that disables Submit on a field an international
physician cannot hold is a wall in front of a doctor filling this in between
patients. So `needed` appears on exactly one field per physician, and nothing
in that path is readable by the validity expression.

A COLLAPSED BOX NEVER HIDES A GUESS. The CV writes values on somebody's behalf.
A section holding one that nobody has confirmed opens itself.
"""

from __future__ import annotations

import pathlib
import re

_LANDING = (pathlib.Path(__file__).resolve().parents[2] / "landing" / "src" / "app"
            / "components")
_STEPS = (_LANDING / "onboarding" / "steps.tsx").read_text(encoding="utf-8")
_PRIMS = (_LANDING / "onboarding" / "primitives.tsx").read_text(encoding="utf-8")
_MODEL = (_LANDING / "onboarding" / "completeness.ts").read_text(encoding="utf-8")
_WIZARD = (_LANDING / "OnboardingWizard.tsx").read_text(encoding="utf-8")


def _strip_tsx_comments(source: str) -> str:
    """Prose beside the code explains the rules; a grep for a rule must not be
    satisfied by the paragraph describing it."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


_STEPS_CODE = _strip_tsx_comments(_STEPS)


# ── Red is scarce ───────────────────────────────────────────────────────────

def test_only_the_identifier_is_ever_marked_needed():
    """`needed` is the one pink marker that is not about a broken value, and it
    is worth having only while it is rare. It goes on the identifier because
    npi_verified is the single largest weight in propose_tier, and on the
    licensure country when that is still unanswered, because until then the
    identifier question cannot be asked at all."""
    marked = re.findall(r'requirement=\{reviewMode && ident\.key === "(\w+)"', _STEPS_CODE)
    assert sorted(set(marked)) == ["countryOfLicensure", "npi", "registrationNumber"]
    # And no field marks itself needed by any other route.
    assert 'requirement="needed"' not in _STEPS_CODE


def test_exactly_two_fields_are_ever_marked_required():
    """The client gate and the server gate agree on two: name and specialty.
    A third marker here would be the page inventing a rule the backend does not
    have, and the physician would believe it."""
    required = re.findall(r'requirement=\{reviewMode && !c\.(\w+)\.trim\(\)\s*\?\s*"required"',
                          _STEPS_CODE)
    assert sorted(required) == ["fullLegalName", "primarySpecialty"]


def test_the_needed_marker_cannot_reach_the_submit_gate():
    """The whole reconciliation. Red says "missing and it matters"; it must not
    be able to say "you may not send this". An international physician has no
    NPI, and the repo is deliberate that a missing one is a review flag."""
    start = _STEPS_CODE.index("const reviewValid")
    end = _STEPS_CODE.index(";", _STEPS_CODE.index("const valid ="))
    gate = _STEPS_CODE[start:end]
    for token in ("requirement", "needed", "ident."):
        assert token not in gate, f"the submit gate reads {token}"


def test_the_identifier_marker_moves_when_no_country_has_been_chosen():
    """isUS treats an unanswered country as the US, so without this a
    consultant in Riyadh is shown a red marker on an NPI field they can never
    hold."""
    assert 'if (!ctx.countrySet) return { key: "countryOfLicensure"' in _MODEL


def test_a_missing_identifier_never_paints_the_input_border():
    """Pink on the border already means a malformed value. Teaching a physician
    that missing and wrong look identical teaches them to ignore both."""
    assert "NeededNote" in _PRIMS
    # The property is about the INPUT's border expression, not about the note:
    # only `error` may darken the shell to pink.
    shell = _PRIMS[_PRIMS.index("export function TextField"):]
    # The expression wraps across lines, so it is bounded by the next property
    # rather than by a newline.
    shell = shell[shell.index("border:"):]
    shell = shell[:shell.index("borderRadius")]
    assert "error ?" in shell
    assert "needed" not in shell, "a missing value paints the field like a broken one"


# ── A collapsed box never hides a guess ─────────────────────────────────────

def test_a_section_opens_itself_when_it_holds_an_unconfirmed_cv_value():
    assert "hasUnconfirmedCv" in _MODEL and "hasUnconfirmedCv" in _STEPS_CODE
    opener = _STEPS_CODE[_STEPS_CODE.index("const openBy"):][:300]
    assert "hasUnconfirmedCv" in opener and "hasNeeded" in opener


def test_collapsing_a_section_does_not_unmount_what_is_inside_it():
    """ChipMultiSelect holds a half-typed entry in local state, so conditional
    rendering would silently bin whatever somebody was in the middle of typing
    when they collapsed a box."""
    sec = _PRIMS[_PRIMS.index("export function OnboardingSection"):]
    sec = sec[:sec.index("\nexport function ")]
    assert 'display: open ? "block" : "none"' in sec
    assert "{open && (" not in sec


# ── The counts have to be true ──────────────────────────────────────────────

def test_an_untouched_repeatable_row_is_not_counted_as_filled():
    """Board certifications, fellowship and residency always hold at least one
    row and that row starts EMPTY. Counting length alone reported an untouched
    form as complete, which is the one thing a completeness summary may never
    do."""
    assert "rowHasContent" in _MODEL
    assert "v.some(rowHasContent)" in _MODEL


def test_the_count_excludes_the_purely_optional_fields():
    """"9 of 28" on a properly filled form reads as failure, and a number that
    lies about the state of the work teaches people to stop reading it."""
    assert 'const counted = fields.filter((x) => w(x.key) !== "optional");' in _MODEL


def test_a_section_with_nothing_to_count_prints_no_count():
    """"0 of 0" reads as a failure state on a box where there is nothing to
    fail at."""
    assert "{total > 0 && (" in _PRIMS


# ── The CV chip tells the truth about what it filled ────────────────────────

def test_every_key_the_cv_fills_is_chipped_or_explained():
    """The chip is the only thing on the page that says "we wrote this, not
    you". It was on six of the thirteen keys the parse writes, so seven values
    appeared in a physician's application with nothing marking them as our
    reading of their CV."""
    filled = set(re.findall(r'fill\("(\w+)"', _WIZARD))
    filled |= set(re.findall(r'filled\.push\("(\w+)"\)', _WIZARD))
    #: Keys whose chip rides a different control than their own label.
    ALLOWED = {
        # `degree` and `qualification` are written together and share one
        # control, which is chipped on whichever of the two that control edits.
        "degree", "qualification",
    }
    chipped = set(re.findall(r'lbl\("(\w+)"', _STEPS_CODE))
    chipped |= set(re.findall(r'autofilled\.has\("(\w+)"\)', _STEPS_CODE))
    missing = sorted(filled - chipped - ALLOWED)
    assert not missing, f"autofilled with no 'from your CV' chip: {missing}"


def test_a_value_the_cv_writes_is_always_shown_to_the_physician():
    """yearsInActivePractice was autofilled from the CV and rendered NOWHERE, so
    a number we inferred about somebody was submitted on their behalf and they
    could neither see it nor correct it."""
    assert "yearsInActivePractice" in _STEPS_CODE
    assert 'label={lbl("yearsInActivePractice"' in _STEPS_CODE


def test_the_second_cv_upload_is_conditional():
    """Offering an upload box two screens after the one that asked for a CV is
    a large part of why this page reads as a form that has forgotten what you
    already gave it."""
    call = _STEPS_CODE[_STEPS_CODE.index("<CvUploadField") - 260:]
    call = call[:call.index("<CvUploadField") + 200]
    assert "c.cvFilename" in call


# ── The boxes themselves ────────────────────────────────────────────────────

def test_every_section_says_why_it_is_being_asked_for():
    """"Why does this matter" is the question the old page never answered, and
    a physician cannot ask it of a box they have not opened, so the answer is
    rendered whether the box is open or shut."""
    whys = re.findall(r'build\("(\w+)", "([^"]+)",', _MODEL)
    assert len(whys) == 3
    sec = _PRIMS[_PRIMS.index("export function OnboardingSection"):]
    sec = sec[:sec.index("\nexport function ")]
    header = sec[sec.index("<button"):sec.index("</button>")]
    assert "{why}" in header, "the why line is inside the collapsible body"
