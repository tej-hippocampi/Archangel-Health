#!/usr/bin/env python3
"""Find (and optionally fix) em dashes in copy a person reads.

Why this is a script and not a `sed -i 's/—/-/g'`:

The em dash is banned in what we SHIP, not in what we write to each other. This
codebase carries roughly 6500 em dashes and the overwhelming majority of them
are inside comments and docstrings explaining why a piece of code is the way it
is. Rewriting those would destroy the reasoning this repo runs on and would not
change a single character any physician ever sees.

So the sweep is structural, never textual:

  * Python is walked with ``tokenize``, so a STRING token is a string and a
    COMMENT token is a comment, provably, with no regex guessing. Docstrings are
    excluded too: a docstring is the codebase talking to itself.
  * JS and TSX are walked with a small scanner that tracks whether it is inside
    a line comment, a block comment, a quoted string or a template literal, so
    the same distinction holds without a full parser.
  * HTML is walked for text nodes outside <script>/<style>.

And the fix is not a blind substitution. An em dash does three different jobs
and each wants a different repair:

    "A — B"   a parenthetical or an aside   ->  "A, B"      (or a full stop)
    "A—B"     a range or a compound         ->  "A-B"
    "— A"     a leading dash in a list      ->  "- A"

``--fix`` applies the mechanical two (tight and leading). Spaced em dashes are
REPORTED, never auto-fixed, because choosing between a comma and a full stop is
a judgment about the sentence and a comma spliced into the wrong clause reads
worse than the dash did.

Usage:
    python3 backend/scripts/purge_em_dashes.py            # report
    python3 backend/scripts/purge_em_dashes.py --fix      # apply the safe ones
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import token as _token
import tokenize
from pathlib import Path
from typing import Iterable, List, Tuple

DASH = "—"          # em dash
EN_DASH = "–"       # en dash, swept the same way when spaced

#: The same two characters written as SOURCE ESCAPES. They render identically
#: in a browser and are invisible to a search for the character itself, which is
#: how ``const NULL_CELL = '\\u2014';`` survived the first sweep and kept putting
#: an em dash in every ungraded cell on the admin physician cards. HTML entities
#: are here for the same reason: the email templates use them.
ESCAPES = {
    "\\u2014": DASH,
    "\\u2013": EN_DASH,
    "&mdash;": DASH,
    "&ndash;": EN_DASH,
}


def _decode_escapes(text: str) -> str:
    for esc, ch in ESCAPES.items():
        text = text.replace(esc, ch)
    return text


#: A dash BETWEEN TWO DIGITS is a numeric range ("135-145 mmol/L", "24-48
#: hours"), not the punctuation tell this sweep exists to remove. It is
#: typographically correct as an en dash and it shows up inside clinical
#: content: lab reference ranges, dose ranges, year spans. Rewriting those
#: edits case data rather than copy, so ranges are exempt from both the report
#: and the repair.
#: Matched in every spelling and BEFORE escapes are decoded, so "24&ndash;48"
#: comes out of the sweep exactly as it went in rather than as "24-48". An HTML
#: entity and the character render the same; rewriting one into the other is a
#: diff with no reader on the other end.
_RANGE = re.compile(
    r"(?:\d|\})(?:[\u2013\u2014]|&[mn]dash;|\\u201[34])(?:\d|\{)")


def _strip_ranges(text: str) -> str:
    return _RANGE.sub("", text)


def _has_dash(text: str) -> bool:
    """True when a READER would see a dash used as PUNCTUATION.

    However it is spelled in source, and ignoring numeric ranges.
    """
    decoded = _strip_ranges(_decode_escapes(text))
    return DASH in decoded or EN_DASH in decoded


ROOT = Path(__file__).resolve().parents[2]

#: Backend modules whose STRINGS reach a person: email bodies and subjects,
#: HTTPException details the client renders verbatim, admin console labels.
#:
#: Whole directories rather than a hand list, because the first version of this
#: list missed the email SUBJECTS. They are passed at the send site in
#: routers/onboarding.py, not by the builder in onboarding_emails.py, so the
#: body was swept and the subject line kept its dash. A physician sees the
#: subject before they see anything else.
#:
#: Safe to sweep wholesale because the sweep only ever touches string literals,
#: never comments or docstrings, and skips raw strings (patterns). Verified
#: before widening that no equality or membership check in these trees compares
#: against a literal containing a long dash.
BACKEND_COPY_GLOBS = [
    "backend/routers/*.py",
    "backend/asclepius/*.py",
    "backend/onboarding_emails.py",
    "backend/notifications.py",
    "backend/community/*.py",
]

#: Whole trees whose strings are copy by default.
FRONTEND_DIRS = ["frontend/asclepius", "landing/src"]

FRONTEND_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".html"}

#: Not copy: fixtures, snapshots, vendored code, build output.
SKIP_PARTS = {"node_modules", "dist", "build", "__pycache__", ".venv", "vendor"}

#: Modules that build CONTENT rather than talk to a person. Two kinds, both
#: excluded for the same reason: the dashes in them are not our voice.
#:
#: Deliverables. ``export.py`` and ``packaging.py`` emit whole files shipped to
#: a buyer, including a Python scaffold whose own ``#`` comment lines sit inside
#: a triple-quoted string and so read as copy to the tokenizer.
#:
#: Clinical case content. The rest render the case a physician labels and the
#: prompt a model is scored on: lab reference ranges ("135-145 mmol/L"), dose
#: ranges, panel headers, question stems, the authored gold answers. Editing
#: those is editing the DATA, not the product's writing, and it changes what
#: models are asked and what the gold set says they should have answered. A
#: physician does read them, and they are still out of scope: an en dash in a
#: lab range is correct typography, not the tell this sweep exists to remove.
SKIP_FILES = {
    "backend/asclepius/export.py",
    "backend/asclepius/packaging.py",
    "backend/asclepius/cases.py",
    "backend/asclepius/case_formats.py",
    "backend/asclepius/gold_cases.py",
    "backend/asclepius/real_cases.py",
    "backend/asclepius/v4_cases.py",
    "backend/asclepius/prompts.py",
    "backend/asclepius/generation.py",
    "backend/asclepius/patient_fixtures.py",
}


# ── Python ───────────────────────────────────────────────────────────────────


def _is_pattern_literal(literal: str) -> bool:
    """A raw string is a regex, not copy, and rewriting one is a live bug.

    The first version of this script turned ``re.compile(r"\\s*[\u2013\u2014]+\\s*")``
    into ``[--]``, which is the character RANGE from hyphen to hyphen. The
    email dash scrubber then matched plain hyphens and would have mangled every
    hyphenated word we send. Raw strings are how this codebase writes patterns,
    so the prefix is the signal.
    """
    prefix = literal[:3].lower()
    return "r" in prefix.split('"')[0].split("'")[0]

def python_copy_hits(path: Path) -> List[Tuple[int, str]]:
    """(line, text) for every string literal containing a dash, docstrings out."""
    src = path.read_text(encoding="utf-8")
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []

    # A docstring is a STRING that is the first real token of a module or of a
    # suite opened by an INDENT. Track that rather than re-parsing with ast, so
    # the line numbers stay exactly the tokenizer's.
    docstring_lines = set()
    expect_doc = True
    for tok in toks:
        if tok.type in (_token.NEWLINE, _token.NL, _token.COMMENT):
            continue
        if tok.type == _token.INDENT:
            expect_doc = True
            continue
        if tok.type == _token.DEDENT:
            continue
        if tok.type == _token.STRING and expect_doc:
            docstring_lines.add(tok.start[0])
        expect_doc = False

    out = []
    for tok in toks:
        if tok.type != _token.STRING:
            continue
        if tok.start[0] in docstring_lines:
            continue
        if _is_pattern_literal(tok.string):
            continue
        if _has_dash(tok.string):
            out.append((tok.start[0], tok.string))
    return out


# ── JS / TSX / HTML ──────────────────────────────────────────────────────────

def _js_quoted_runs(line: str) -> List[Tuple[int, int]]:
    """Quoted regions of ONE line, and the code region before a // comment.

    Line-based on purpose. Two hand-rolled whole-file JS lexers failed here
    before this one, both in the same way: JavaScript cannot be tokenized
    without deciding whether a `/` opens a regex or is a division, and both
    versions eventually mistook a regex literal containing a quote or a
    backtick for a string. Each time, the scanner fell out of phase and started
    reading COMMENT PROSE as copy, which in a --fix run means rewriting the
    reasoning this codebase is built on.

    A line is a small enough unit that none of that matters, and the failure
    mode flips from "corrupts a comment" to "misses a dash", which is the right
    way round. The one thing it cannot see is a template literal spanning
    lines; the sweep reports those separately rather than guessing.
    """
    runs: List[Tuple[int, int]] = []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c == "/" and i + 1 < n and line[i + 1] == "/":
            break                      # rest of the line is a comment
        if c in "'\"`":
            j = i + 1
            while j < n and line[j] != c:
                j += 2 if line[j] == "\\" else 1
            if j >= n:
                # Unterminated on this line: an apostrophe in prose, or the
                # start of a multi-line template. Either way, not our business.
                i += 1
                continue
            runs.append((i + 1, j))
            i = j + 1
            continue
        i += 1
    return runs


def _js_copy_lines(src: str):
    """Yield (line_no, line_text, quoted_runs) for lines that are not comments."""
    in_block = False
    for ln, line in enumerate(src.splitlines(), 1):
        stripped = line.lstrip()
        if in_block:
            if "*/" in line:
                in_block = False
                after = line.split("*/", 1)[1]
                if after.strip():
                    yield ln, after, _js_quoted_runs(after)
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                in_block = True
            continue
        if stripped.startswith(("//", "*")):
            continue
        yield ln, line, _js_quoted_runs(line)


def _html_text_spans(src: str) -> List[Tuple[int, int]]:
    """Character spans of text nodes and of attribute values, script/style out."""
    spans: List[Tuple[int, int]] = []
    i, n = 0, len(src)
    while i < n:
        lt = src.find("<", i)
        if lt == -1:
            spans.append((i, n))
            break
        if lt > i:
            spans.append((i, lt))
        if src.startswith("<!--", lt):
            # An HTML comment is the codebase talking to itself, exactly like a
            # // comment in the JS scanner above. Skip it whole.
            end = src.find("-->", lt)
            i = n if end == -1 else end + 3
            continue
        low = src[lt:lt + 8].lower()
        if low.startswith("<script") or low.startswith("<style"):
            closer = "</script" if low.startswith("<script") else "</style"
            end = src.lower().find(closer, lt)
            i = n if end == -1 else end
            continue
        gt = src.find(">", lt)
        if gt == -1:
            break
        spans.append((lt, gt + 1))   # the tag itself, for attribute copy
        i = gt + 1
    return spans


def frontend_copy_hits(path: Path) -> List[Tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    out: List[Tuple[int, str]] = []
    if path.suffix == ".html":
        for start, end in _html_text_spans(src):
            chunk = src[start:end]
            if _has_dash(chunk):
                out.append((src.count("\n", 0, start) + 1, chunk.strip()[:160]))
        return out
    for ln, line, runs in _js_copy_lines(src):
        for a, b in runs:
            chunk = line[a:b]
            if _has_dash(chunk):
                out.append((ln, chunk.strip()[:160]))
    return out


# ── The mechanical repairs ───────────────────────────────────────────────────

SPACED = re.compile(r"[ \t]*[\u2013\u2014][ \t]*")

#: A short lead-in before a dash is a LABEL ("Network error — check your
#: connection"), and a label wants a colon. Anything longer is a clause, and a
#: clause wants a comma. 32 characters is where the two stop being confusable in
#: this codebase's copy; it was chosen by reading the hits, not guessed.
LABEL_MAX = 32

#: Words that cannot follow a colon. Checked against the text AFTER the dash.
_CONJUNCTION = re.compile(
    r"(?:so|and|but|or|then|yet|because|which|while|though|nor|for|it|they|we|you|"
    r"that|this|he|she|welcome)\b", re.IGNORECASE)


def repair(text: str, *, spaced: bool = False) -> str:
    """Mechanical repairs only. ``spaced`` opts into the judgment-y one.

    Escaped dashes are decoded up front so one set of rules covers both
    spellings. The output carries plain ASCII where a dash was, so nothing that
    needed escaping in the first place is reintroduced.
    """
    # Ranges are hidden FIRST, before escapes are decoded, so they survive
    # byte for byte. The sentinel is a NUL-delimited index, which cannot occur
    # in source and which none of the rules below can see.
    ranges: List[str] = []

    def _hide(m: "re.Match[str]") -> str:
        ranges.append(m.group(0))
        return "\x00%d\x00" % (len(ranges) - 1)

    out = _decode_escapes(_RANGE.sub(_hide, text))
    for d in (DASH, EN_DASH):
        # A literal that is ONLY a dash is a table placeholder for "no value".
        if out.strip() == d:
            return out.replace(d, "-")
        # Leading dash on its own line, or opening a literal: a bullet, or the
        # dash in front of an email signature. A signature reads better without
        # it; a bullet needs the hyphen.
        out = out.replace("\n" + d + " ", "\n- ")
        if out.startswith(d + " "):
            out = out[2:]
        # Tight dash between word characters: a range or a compound.
        i = 0
        while True:
            i = out.find(d, i)
            if i == -1:
                break
            before = out[i - 1] if i else ""
            after = out[i + 1] if i + 1 < len(out) else ""
            if before and after and not before.isspace() and not after.isspace():
                out = out[:i] + "-" + out[i + 1:]
            i += 1

    def _restore(value: str) -> str:
        for i, original in enumerate(ranges):
            value = value.replace("\x00%d\x00" % i, original)
        return value

    if not spaced:
        return _restore(out)

    # The spaced dash, which is the one that needs a reader. Choose between a
    # colon and a comma from the shape of what comes BEFORE it, measured from
    # the last sentence break rather than from the start of the literal, so a
    # dash in the third sentence is judged on its own sentence.
    def _sub(m: "re.Match[str]") -> str:
        head = out[: m.start()]
        tail = out[m.end():]
        # Strip the quote/JSX punctuation a Python literal starts and ends with,
        # so "is there anything before this dash" means what it says.
        lead = head.lstrip("\"'`([{fr ")
        rest = tail.rstrip("\"'`)]} ")

        # A dash with nothing on one side of it is not punctuation inside a
        # sentence, it is a JOINER between two runtime values, and the three
        # cases have to be told apart. Getting this wrong is not cosmetic:
        # the first version of this rule turned `" - ".join(certs)` into
        # `"".join(certs)`, which silently concatenated a physician's board
        # certifications with no separator at all, and turned
        # `'Step ' + n + ' - they diverge'` into `'Step 2they diverge'`.
        if not lead and not rest:
            return ", "                    # the fragment IS the joiner
        if not lead:
            # Dash opens the fragment. With no space in front of it inside the
            # literal it is a signature dash ("- Tej & Aryaa"), which reads
            # better gone. With a space, the literal is the tail of a
            # concatenation and still needs its separator.
            #
            # Read off the MATCH, not off `head`. The pattern is greedy on the
            # whitespace in front of the dash, so by the time we are here that
            # space is inside m.group(0) and `head` has already lost it. And it
            # cannot be read off the match offset either, because the two
            # callers hand this function different things: the Python walk
            # passes the literal WITH its quotes and prefix, the JS walk passes
            # only the text between them.
            return ", " if m.group(0)[:1] in (" ", "\t") else ""
        if not rest:
            # Dash closes the fragment. Usually decoration ("- skip -"), but in
            # Python two ADJACENT literals concatenate, so a trailing dash can
            # equally be the separator to the next one:
            #
            #     _p("...and write to us - "
            #        "we'll pick it back up with you.")
            #
            # Dropping it outright ran those two words together, and restoring
            # only the space left a run-on ("write to us we'll pick it back
            # up"). The dash was doing a clause break, so a comma is what
            # replaces it. A dash with NO trailing space is decoration at the
            # end of a label ("- skip -") and simply goes.
            return ", " if m.group(0)[-1:] in (" ", "\t") else ""
        if head.rstrip().endswith((",", ":", ";")):
            return " "                     # already punctuated; the dash is noise
        # A colon cannot introduce a conjunction. "...from admission - so the
        # creatinine rise" has a short lead and would score as a label, but
        # "admission: so the" is not English. The word AFTER the dash overrules
        # the length of the words before it.
        if _CONJUNCTION.match(rest):
            return ", "
        seg = re.split(r"[.!?\n]", lead)[-1].strip()
        return ": " if len(seg) <= LABEL_MAX else ", "

    return _restore(SPACED.sub(_sub, out))


# ── Walk ─────────────────────────────────────────────────────────────────────

def targets() -> Iterable[Path]:
    for pattern in BACKEND_COPY_GLOBS:
        head, _, tail = pattern.rpartition("/")
        base = ROOT / head
        if not base.exists():
            continue
        for p in sorted(base.glob(tail)):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            if str(p.relative_to(ROOT)) in SKIP_FILES:
                continue
            yield p
    for d in FRONTEND_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix not in FRONTEND_SUFFIXES:
                continue
            if SKIP_PARTS & set(p.parts):
                continue
            yield p


def hits_for(path: Path) -> List[Tuple[int, str]]:
    return python_copy_hits(path) if path.suffix == ".py" else frontend_copy_hits(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true",
                    help="apply the two mechanical repairs (tight and leading)")
    ap.add_argument("--spaced", action="store_true",
                    help="also rewrite spaced dashes to a colon or a comma")
    args = ap.parse_args()

    total = 0
    fixed_files = 0
    for path in targets():
        found = hits_for(path)
        if not found:
            continue
        if args.fix:
            src = path.read_text(encoding="utf-8")
            # Repair only inside copy spans, so a comment's dash is untouched.
            if path.suffix == ".py":
                new = src
                for _, lit in found:
                    r = repair(lit, spaced=args.spaced)
                    if r != lit:
                        new = new.replace(lit, r)
            else:
                if path.suffix == ".html":
                    spans = _html_text_spans(src)
                    pieces, last = [], 0
                    for start, end in spans:
                        pieces.append(src[last:start])
                        pieces.append(repair(src[start:end], spaced=args.spaced))
                        last = end
                    pieces.append(src[last:])
                    new = "".join(pieces)
                else:
                    # Rebuild line by line so a comment is physically incapable
                    # of being touched: only the quoted runs are rewritten, and
                    # comment lines never yield a run at all.
                    lines = src.splitlines(keepends=True)
                    for ln, line, runs in _js_copy_lines(src):
                        if not runs:
                            continue
                        raw = lines[ln - 1]
                        newline_suffix = raw[len(raw.rstrip("\r\n")):]
                        body = raw[: len(raw) - len(newline_suffix)]
                        rebuilt, last = [], 0
                        for a, b in runs:
                            rebuilt.append(body[last:a])
                            rebuilt.append(repair(body[a:b], spaced=args.spaced))
                            last = b
                        rebuilt.append(body[last:])
                        lines[ln - 1] = "".join(rebuilt) + newline_suffix
                    new = "".join(lines)
            if new != src:
                path.write_text(new, encoding="utf-8")
                fixed_files += 1
            found = hits_for(path)
        if found:
            rel = path.relative_to(ROOT)
            for line, text in found:
                print(f"{rel}:{line}: {' '.join(text.split())[:140]}")
            total += len(found)

    if args.fix:
        print(f"\nrewrote {fixed_files} file(s).", file=sys.stderr)
    print(f"{total} spaced dash(es) remaining, each needs a human comma or full stop.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
