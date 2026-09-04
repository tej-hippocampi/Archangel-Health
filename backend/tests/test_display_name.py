"""A physician's own name, on their own dashboard.

The reported bug: an account created from ``angad18.bhatia@gmail.com`` was
greeted as **"Dr. Angad18 Bhatia"**. Two separate defects lined up to produce
it, and fixing either alone leaves the other live, so both are pinned here.

  1. The account had no ``full_name``, so the portal fell back to title-casing
     the email local part. That fallback kept the digits (``\\b\\w`` only
     uppercases a leading character, it does not filter) and volunteered an
     honorific nobody had verified.

  2. ``provision_user`` wrote ``full_name = ?`` unconditionally on its UPDATE
     branch, so a re-onboard carrying no name blanked a good one and pushed the
     account into defect 1. ``organization`` two lines below had used COALESCE
     for exactly this reason since it was written.

The rule the fallback now follows: digits are not part of a name, and an email
address is not evidence that its owner is a doctor. When the server knows the
name it wins outright, honorific and all, because that is the one place a
verified honorific can come from.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

_FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "asclepius"
JS = (_FRONTEND / "asclepius.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    """Same brace walk the tour DOM tests use, so this runs the SHIPPED source."""
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
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _names(users: list[dict]) -> list[str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    script = (
        "let state = {};\n"
        + _extract_function(JS, "railDisplayName") + "\n"
        + "const out = " + json.dumps(users) + ".map((u) => { state = { user: u }; "
          "return railDisplayName(); });\n"
        + "console.log(JSON.stringify(out));\n"
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_reported_case_no_longer_puts_a_number_in_a_physicians_name():
    assert _names([{"email": "angad18.bhatia@gmail.com"}]) == ["Angad Bhatia"]


def test_an_email_derived_name_never_claims_the_person_is_a_doctor():
    """We have not verified anything about a mailbox. Do not award a title."""
    for name in _names([
        {"email": "angad18.bhatia@gmail.com"},
        {"email": "r.kessler@lakeshorenephro.com"},
        {"email": "jsmith@example.org"},
    ]):
        assert not name.lower().startswith("dr"), name


def test_the_server_name_wins_and_keeps_its_honorific():
    """The one place a "Dr." may come from is a name a human actually gave us."""
    assert _names([
        {"email": "angad18.bhatia@gmail.com", "full_name": "Dr. Angad Bhatia"},
        {"email": "rk@y.com", "name": "Rachel Kessler"},
        # Blank is not a name, so this one falls through to the email, whose
        # local part is a single letter and is not a name either.
        {"email": "x@y.com", "full_name": "   "},
    ]) == ["Dr. Angad Bhatia", "Rachel Kessler", "Clinician"]


def test_a_local_part_with_nothing_nameable_falls_back_to_a_neutral_word():
    assert _names([
        {"email": "12345@example.com"},
        {"email": "@example.com"},
        {},
    ]) == ["Clinician", "Clinician", "Clinician"]


def test_provision_user_cannot_blank_a_name_it_was_not_given():
    """Defect 2, read off the shipped SQL rather than round-tripped.

    A store-level round trip would need a provisioned account and would pass
    for the wrong reason if the caller happened to supply a name. The clause
    itself is the invariant.
    """
    store = (pathlib.Path(__file__).resolve().parents[1]
             / "asclepius" / "store.py").read_text(encoding="utf-8")
    assert "full_name = COALESCE(NULLIF(?, ''), full_name)" in store, (
        "provision_user's UPDATE writes full_name unconditionally again; a "
        "re-onboard with no name will blank a good one"
    )
