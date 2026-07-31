"""Rendered-appearance gate for the Asclepius eval portal.

Every other frontend test in this repo asserts on source text or on DOM built
under a shim. Both are blind to the cascade — and two defects shipped through a
fully green suite precisely there:

  * ``.asc-sev-pill`` carries ``text-transform: capitalize``, correct back when
    those pills rendered raw enum values (`accuracy` -> `Accuracy`). §11 made the
    options plain-language sentences, so the same rule silently produced "Got The
    Facts Right" — a form label again, the exact thing §11 removes. The JS emitted
    the right string; the CSS was defensible on its own; only the composition was
    wrong.
  * The rubric slider painted in the browser's default blue, in a palette whose
    own header comment says there is no blue. Nothing in the source says "blue"
    anywhere — it is a user-agent default that only exists once painted.

So the two tests that matter here are the GENERAL ones, not pins for those two
bugs: ``test_no_off_palette_colour_is_painted`` and
``test_no_capitalize_rule_mangles_a_sentence``. Each catches its defect without
anyone having known to look for it.

A warning to whoever edits them, learned the expensive way. The first version of
both was VACUOUS — it passed against the very bugs it was written for:

  * The colour guard walked ``getComputedStyle``. An unstyled range input
    computes ``accent-color: auto``; the blue exists only in the framebuffer, so
    there was no colour string to find. It now reads pixels.
  * The text guard compared ``textContent`` against the authored string.
    ``text-transform`` is paint-only, so both sides were the same source string
    and the comparison could never fail. It now asserts the computed property.

The lesson generalises: a rendering test that reads the DOM is usually still a
source test wearing a costume. If the defect lives in the paint, assert on the
paint. Every test in this file is mutation-checked against the defect it claims
to cover — please keep it that way.

Skips cleanly when playwright or a browser is unavailable, so it never blocks a
contributor who has not installed them.
"""

from __future__ import annotations

import colorsys
import io
import os
import pathlib
import re

import pytest

from tests import _asclepius_harness as harness

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)


# ─── palette ──────────────────────────────────────────────────────────────────
# The locked design system is green / orange / pink / lime over teal-shifted ink
# and greys. There is no blue in it, and none should ever be painted — a blue is
# always either a user-agent default that escaped (the slider) or a token from
# some other product that leaked in.
_BLUE_HUE_RANGE = (185.0, 290.0)     # cyan through violet, in degrees
_SATURATION_FLOOR = 0.18             # below this it is a grey, not a hue
_ALPHA_FLOOR = 0.05                  # fully transparent paints nothing

_RGB_RE = re.compile(
    r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.%]+))?\s*\)"
)

# Properties a browser can paint a colour through. `box-shadow` is excluded: its
# computed value is a compound string and the design system's shadows are all
# ink-based, so parsing it would add false positives without adding coverage.
_COLOUR_PROPS = (
    "color", "backgroundColor", "borderTopColor", "borderRightColor",
    "borderBottomColor", "borderLeftColor", "outlineColor", "accentColor",
    "textDecorationColor", "caretColor", "fill", "stroke",
)


def _parse_colour(value: str):
    """(hue_deg, saturation, alpha) for an rgb/rgba string, else None."""
    m = _RGB_RE.search(value or "")
    if not m:
        return None
    r, g, b = (float(m.group(i)) / 255.0 for i in (1, 2, 3))
    raw_a = m.group(4)
    if raw_a is None:
        alpha = 1.0
    elif raw_a.endswith("%"):
        alpha = float(raw_a[:-1]) / 100.0
    else:
        alpha = float(raw_a)
    hue, _light, sat = colorsys.rgb_to_hls(r, g, b)
    return hue * 360.0, sat, alpha


def _is_off_palette_blue(value: str) -> bool:
    parsed = _parse_colour(value)
    if not parsed:
        return False
    hue, sat, alpha = parsed
    if alpha < _ALPHA_FLOOR or sat < _SATURATION_FLOOR:
        return False
    return _BLUE_HUE_RANGE[0] <= hue <= _BLUE_HUE_RANGE[1]


def _blue_pixels(png_bytes: bytes) -> int:
    """How many PAINTED pixels are off-palette blue.

    Computed style is not enough and this is the reason: an unstyled range input
    computes ``accent-color: auto``, and the browser then paints it blue from an
    internal default. There is no colour string anywhere in the CSSOM to catch —
    the only place that blue exists is the framebuffer. This decodes the
    screenshot and counts it there.

    ``getcolors`` returns one entry per DISTINCT colour, so this is a few hundred
    hue conversions rather than one per pixel.
    """
    from PIL import Image                                          # noqa: PLC0415

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    counts = img.getcolors(maxcolors=1 << 24) or []
    total = 0
    for count, (r, g, b) in counts:
        hue, light, sat = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        if (_BLUE_HUE_RANGE[0] <= hue * 360.0 <= _BLUE_HUE_RANGE[1]
                and sat > 0.25 and 0.12 < light < 0.9):
            total += count
    return total


# Antialiasing on text and rounded corners throws a handful of stray hues, so a
# bare >0 would be flaky. A real off-palette control paints orders of magnitude
# more than this: the blue slider track alone is ~160x4px plus its thumb.
_BLUE_PIXEL_TOLERANCE = 120


# ─── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def page_url(tmp_path_factory) -> str:
    return "file://" + str(harness.build(tmp_path_factory.mktemp("asc_harness")))


def _installed_chromiums() -> list:
    """Chromium binaries sitting under PLAYWRIGHT_BROWSERS_PATH.

    A pinned browser pool can hold a different build number than the installed
    playwright expects, and the default resolution then fails outright. CI runs
    ``playwright install chromium`` so the default works there; this keeps the
    test from silently skipping everywhere else, which would turn the whole file
    into decoration.
    """
    root = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if not root or not pathlib.Path(root).is_dir():
        return []
    base = pathlib.Path(root)
    return [
        str(p)
        for pattern in (
            "chromium-*/chrome-linux/chrome",
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        )
        for p in sorted(base.glob(pattern))
        if p.is_file()
    ]


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        attempts, last = [None] + _installed_chromiums(), None
        for exe in attempts:
            try:
                b = p.chromium.launch(**({"executable_path": exe} if exe else {}))
            except Exception as exc:                              # noqa: BLE001, PERF203
                last = exc
                continue
            yield b
            b.close()
            return
        pytest.skip(f"no launchable chromium for playwright: {last}")


@pytest.fixture
def page(browser, page_url):
    pg = browser.new_page(viewport={"width": 1280, "height": 1000})
    errors: list = []
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
          if m.type == "error" else None)
    pg.goto(page_url)
    # Short, explicit timeout with a real message. A harness that renders nothing
    # would otherwise stall every test in the file for the default 30s apiece and
    # report as a wall of opaque timeouts.
    try:
        pg.wait_for_selector(".asc-rubric-stem", timeout=5000)
    except Exception as exc:                                      # noqa: BLE001
        pg.close()
        raise AssertionError(
            "the harness rendered nothing — the page never produced "
            f".asc-rubric-stem. Console: {errors}"
        ) from exc
    pg.errors = errors                                            # type: ignore[attr-defined]
    yield pg
    pg.close()


# ═════════════════════════════════════════════════════════════════════════════
#  Harness sanity — this one protects every other test in the file
# ═════════════════════════════════════════════════════════════════════════════
def test_the_harness_actually_rendered_the_components(page):
    """If the harness ever produces a blank page, nearly every assertion below
    becomes vacuously true: empty collections satisfy "no offenders", and two
    empty lists compare equal. That failure would be silent and total.

    So: assert the page contains the things the rest of the file interrogates,
    before interrogating them.
    """
    counts = page.evaluate(
        """() => ({
          answers: document.querySelectorAll('#ascAnswers .asc-answer').length,
          sections: document.querySelectorAll('.asc-substage').length,
          steps: document.querySelectorAll('[data-step-idx]').length,
          openStep: document.querySelectorAll('.asc-step-original-sum').length,
          axisPills: document.querySelectorAll('.asc-axis-row .asc-sev-pill').length,
          tiers: document.querySelectorAll('.asc-rubric-tier-name').length,
          stems: document.querySelectorAll('.asc-rubric-stem-toggle').length,
          badges: document.querySelectorAll('.asc-exp-badge').length,
          sliders: document.querySelectorAll('.asc-rubric-scale input[type=range]').length,
        })"""
    )
    assert counts["answers"] == 2, counts
    assert counts["sections"] == 3, counts           # compare / reasoning / rubric
    assert counts["steps"] == 4, counts
    assert counts["openStep"] == 1, counts           # step 2 is expanded
    assert counts["axisPills"] == 12, counts         # 6 options x 2 criterion cards
    assert counts["tiers"] == 6, counts
    assert counts["stems"] == 2, counts              # both polarities painted
    assert counts["badges"] == 1, counts
    assert counts["sliders"] == 2, counts


# ═════════════════════════════════════════════════════════════════════════════
#  The two general guards — these are the point of this file
# ═════════════════════════════════════════════════════════════════════════════
def test_no_off_palette_colour_is_painted(page):
    """§0.3: the design system is locked, and there is no blue in it.

    Checks the FRAMEBUFFER, not the CSSOM. The rubric slider that shipped blue
    computes ``accent-color: auto`` — the browser paints its own default and no
    colour string ever appears in computed style, so a CSSOM walk is blind to it
    by construction. An earlier version of this test did exactly that walk and
    passed against the bug it was written for.

    Catches an un-themed native control, a hard-coded hex from another product,
    or a drifting token — none of which anyone has to anticipate.
    """
    painted = _blue_pixels(page.screenshot(full_page=True))
    assert painted <= _BLUE_PIXEL_TOLERANCE, (
        f"{painted} off-palette blue pixels are painted (tolerance "
        f"{_BLUE_PIXEL_TOLERANCE} for antialiasing). The design system is "
        "green / orange / pink / lime over ink — a blue is either a user-agent "
        "default that escaped or a token from somewhere else."
    )

    # Belt and braces: an explicitly DECLARED blue would show up in computed
    # style even where it paints nothing yet (a :hover rule, a hidden element).
    declared = page.evaluate(
        """(props) => {
          const out = [];
          for (const el of document.querySelectorAll('*')) {
            const cs = getComputedStyle(el);
            for (const p of props) {
              const v = cs[p];
              if (v) out.push({
                sel: el.tagName.toLowerCase() + (el.className && typeof el.className === 'string'
                       ? '.' + el.className.trim().split(/\\s+/).join('.') : ''),
                prop: p, value: v,
              });
            }
          }
          return out;
        }""",
        list(_COLOUR_PROPS),
    )
    blues = [d for d in declared if _is_off_palette_blue(d["value"])]
    assert not blues, (
        "off-palette colour declared:\n  "
        + "\n  ".join(f"{d['sel']} {d['prop']}: {d['value']}" for d in blues[:12])
    )


def test_no_capitalize_rule_mangles_a_sentence(page):
    """``text-transform: capitalize`` Title-Cases prose, and prose is never
    meant to be Title-Cased.

    This is the general form of the §11 defect: ``.asc-sev-pill`` capitalized,
    which was right when those pills showed raw enum values (`accuracy` ->
    `Accuracy`) and wrong the moment the options became sentences.

    It has to be checked as a COMPUTED PROPERTY. ``text-transform`` is paint-only
    — ``textContent`` and ``Range.toString()`` both return the source string — so
    comparing "rendered" text against authored text compares the source with
    itself and can never fail. An earlier version of this test did that.

    ``uppercase`` is deliberately allowed: mono chrome labels use it as an idiom,
    and it is not ambiguous the way Title Case is.
    """
    offenders = page.evaluate(
        """() => {
          const out = [];
          for (const el of document.querySelectorAll('*')) {
            if (getComputedStyle(el).textTransform !== 'capitalize') continue;
            // Own text only — a container inherits the rule but the leaf paints it.
            const own = Array.from(el.childNodes)
              .filter((n) => n.nodeType === 3).map((n) => n.nodeValue).join('').trim();
            if (own.split(/\\s+/).filter(Boolean).length > 2) {
              out.push({ sel: el.tagName.toLowerCase() + (el.className && typeof el.className === 'string'
                           ? '.' + el.className.trim().split(/\\s+/).join('.') : ''), text: own });
            }
          }
          return out;
        }"""
    )
    assert not offenders, (
        "text-transform: capitalize is Title-Casing prose:\n  "
        + "\n  ".join(f"{o['sel']}: {o['text']!r}" for o in offenders)
    )


def test_rendered_labels_match_the_authored_text(page):
    """The JS half of the same concern: what the source authors is what the DOM
    carries. (Paint-level mangling is covered by the capitalize test above —
    ``textContent`` cannot see it.)"""
    # Scoped to the FIRST axis row: the harness paints two criterion cards so both
    # stem polarities are covered, and an unscoped selector returns the options twice.
    rendered_axes = page.evaluate(
        """() => Array.from(document.querySelector('.asc-axis-row').children)
             .map((b) => b.textContent)"""
    )
    authored_axes = list(harness.axis_labels().values())
    assert len(authored_axes) == 6, (
        "the AXIS_LABELS parser returned nothing useful — an empty list would\n"
        "make the comparison below vacuously true"
    )
    assert rendered_axes == authored_axes, (
        f"authored: {authored_axes}\nrendered: {rendered_axes}"
    )

    rendered_tiers = page.evaluate(
        """() => Array.from(document.querySelector('.asc-rubric-tier-row').children)
             .map((e) => e.querySelector('.asc-rubric-tier-name').textContent)"""
    )
    assert rendered_tiers == harness.tier_labels() == ["Critical", "Major", "Minor"]

    assert page.evaluate(
        "document.querySelector('.asc-rubric-stem-toggle').textContent") == "must never"
    assert page.evaluate(
        "document.querySelector('.asc-rubric-stem-lead').textContent") == "A correct answer"


# ═════════════════════════════════════════════════════════════════════════════
#  Layout facts that only a real engine can settle
# ═════════════════════════════════════════════════════════════════════════════
def test_no_console_errors_rendering_the_portal(page):
    assert page.errors == [], page.errors
    assert page.evaluate("window.__harnessErrors") == []


def test_answer_cards_are_symmetric_and_side_by_side(page):
    """§3, the stagger regression, measured rather than inferred. The legend used
    to be a third child of a ``1fr 1fr`` grid, which pushed B onto row 2."""
    geo = page.evaluate(
        """() => {
          const g = document.querySelector('#ascAnswers');
          const [a, b] = g.children;
          const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
          return { n: g.children.length, topDelta: Math.abs(ra.top - rb.top),
                   widthDelta: Math.abs(ra.width - rb.width),
                   sideBySide: rb.left >= ra.right - 1,
                   leftBorders: [getComputedStyle(a).borderLeftColor,
                                 getComputedStyle(b).borderLeftColor] };
        }"""
    )
    assert geo["n"] == 2, "the answers grid has a child that is not an answer card"
    assert geo["sideBySide"], "the answer cards are not side by side"
    assert geo["topDelta"] < 1, "the answer cards' tops are not aligned"
    assert geo["widthDelta"] < 1, "the answer cards are not equal width"
    # Both are model output, so both carry the orange accent — symmetry is the point.
    assert geo["leftBorders"][0] == geo["leftBorders"][1]


def test_original_bar_truncates_at_the_true_edge(page):
    """§8: the full text goes into the DOM and CSS ellipsises where the bar
    actually ends. A JS character cut is right at exactly one width."""
    geo = page.evaluate(
        """() => {
          const sum = document.querySelector('.asc-step-original-sum');
          const pv = sum.querySelector('.asc-step-original-preview');
          return { ratio: pv.getBoundingClientRect().width / sum.getBoundingClientRect().width,
                   ellipsised: pv.scrollWidth > pv.clientWidth,
                   text: pv.textContent };
        }"""
    )
    assert geo["ellipsised"], "the preview is not being ellipsised by CSS"
    assert geo["ratio"] > 0.7, (
        f"the preview only fills {geo['ratio']:.0%} of the bar — it is not "
        "expanding to the true edge"
    )
    assert "…" not in geo["text"], "JS inserted an ellipsis; CSS should own that"


def test_experience_badge_reads_as_two_groups(page):
    """§13: ``space-between`` over three children marooned the two links at
    opposite edges. Grouped, it reads label-left / controls-right."""
    geo = page.evaluate(
        """() => {
          const badge = document.querySelector('.asc-exp-badge');
          const links = badge.querySelector('.asc-exp-links');
          const [a, b] = links.children;
          return { children: badge.children.length,
                   gap: b.getBoundingClientRect().left - a.getBoundingClientRect().right,
                   divider: getComputedStyle(b).borderLeftWidth };
        }"""
    )
    assert geo["children"] == 2
    assert geo["gap"] < 4, "the two links are not adjacent — they read as separate controls"
    assert geo["divider"] != "0px", "the hairline divider is not drawn"


def test_specificity_chip_sits_at_the_edge_not_mid_sentence(page):
    """§9: the specific/vague chip is a verdict ON the sentence, not a word in
    it. Mid-line it read as a third word after the toggle."""
    geo = page.evaluate(
        """() => {
          const stem = document.querySelector('.asc-rubric-stem');
          const chip = stem.querySelector('.asc-rubric-spec');
          const toggle = stem.querySelector('.asc-rubric-stem-toggle');
          return { rightGap: stem.getBoundingClientRect().right - chip.getBoundingClientRect().right,
                   fromToggle: chip.getBoundingClientRect().left - toggle.getBoundingClientRect().right };
        }"""
    )
    assert geo["rightGap"] < 2, "the chip is not aligned to the field's right edge"
    assert geo["fromToggle"] > 40, "the chip is still crowding the sentence stem"


def test_no_control_overflows_its_card(page):
    """No interactive control may spill outside the card that contains it.

    Generalised from a real regression: §10 moved the slider, the points readout
    and the auto-fail badge onto one line beside the question. That fits a
    desktop card and does not fit a phone — below ~400px the row ran past the
    card edge and clipped the auto-fail badge, the one piece of state on that
    card a physician must not miss.

    It caused NO horizontal page scroll, so the whole-page check was blind to it;
    an ancestor clipped the overflow instead. The only way to see it is to
    compare each control against the box that is supposed to contain it.
    """
    for width in (1280, 1024, 768, 480, 390, 320):
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(140)
        spills = page.evaluate(
            """() => {
              const out = [];
              for (const card of document.querySelectorAll('.asc-card, .asc-rubric-focus')) {
                const cs = getComputedStyle(card);
                const box = card.getBoundingClientRect();
                const padL = parseFloat(cs.paddingLeft) || 0;
                const padR = parseFloat(cs.paddingRight) || 0;
                const left = box.left + padL, right = box.right - padR;
                for (const el of card.querySelectorAll(
                       'button, input, select, textarea, .asc-badge, .asc-rubric-pts')) {
                  if (!el.offsetParent && el.offsetWidth === 0) continue;   // hidden
                  const r = el.getBoundingClientRect();
                  if (r.width === 0) continue;
                  const over = Math.max(r.right - right, left - r.left);
                  if (over > 1.5) out.push({
                    el: el.tagName.toLowerCase() + (el.className && typeof el.className === 'string'
                          ? '.' + el.className.trim().split(/\\s+/).join('.') : ''),
                    text: (el.textContent || '').trim().slice(0, 40),
                    over: Math.round(over),
                  });
                }
              }
              return out;
            }"""
        )
        assert not spills, (
            f"at {width}px, controls spill outside their card (they get clipped, "
            f"and the page shows no scrollbar to reveal it):\n  "
            + "\n  ".join(f"{s['el']} {s['text']!r} by {s['over']}px" for s in spills)
        )


def test_page_never_scrolls_horizontally(page):
    for width in (1280, 1024, 768, 390):
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(120)
        assert not page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        ), f"the page scrolls horizontally at {width}px"


# ═════════════════════════════════════════════════════════════════════════════
#  The specialty popover — stacking is only decidable once painted
# ═════════════════════════════════════════════════════════════════════════════
def test_popover_covers_every_piece_of_chrome(page):
    """§2: a modal the sticky header paints over is not a modal, and its nav
    stays clickable — which would let a physician navigate away from the
    first-entry picker that §2 deliberately makes non-dismissable.

    Hit-testing, not z-index arithmetic: stacking contexts make the declared
    value an unreliable proxy for what is actually on top.
    """
    page.evaluate(
        # Arrow body, NOT a bare expression: the picker returns a promise that only
        # settles when the physician picks or dismisses, and page.evaluate AWAITS a
        # returned promise — handing back the promise itself would hang the test.
        "() => { renderSpecialtyPicker({ dismissable: true }); }"
    )
    page.wait_for_selector(".asc-sheet")

    over_nav = page.evaluate(
        """() => { const b = document.querySelector('.asc-nav-btn');
                   const r = b.getBoundingClientRect();
                   const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                   return hit === b ? 'NAV-CLICKABLE' : hit.className; }"""
    )
    assert "asc-sheet" in over_nav, f"the header nav is not covered: {over_nav}"
    assert page.evaluate("getComputedStyle(document.body).overflow") == "hidden", (
        "the page behind the modal still scrolls"
    )
    assert page.evaluate(
        "document.querySelector('.asc-sheet').getAttribute('aria-modal')") == "true"

    # Three options, one card each, no nested call-to-action restating the tap.
    assert page.evaluate("document.querySelectorAll('.asc-spec-card').length") == 3
    assert page.evaluate(
        "document.querySelectorAll('.asc-spec-grid .asc-btn-primary').length") == 0

    # Dismissable here, and it cleans up after itself.
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.evaluate("document.querySelectorAll('.asc-sheet').length") == 0
    assert page.evaluate("getComputedStyle(document.body).overflow") != "hidden", (
        "the scroll lock leaked after the sheet closed"
    )


def test_popover_scrim_actually_dims_what_is_behind_it(page):
    """The hit-test proves the sheet is on top; this proves it is VISIBLY on top.
    An element can win the hit-test while painting nothing (a transparent
    overlay), which would read to the physician as a broken modal."""
    before = page.screenshot(clip={"x": 0, "y": 0, "width": 400, "height": 50})
    page.evaluate(
        # Arrow body, NOT a bare expression: the picker returns a promise that only
        # settles when the physician picks or dismisses, and page.evaluate AWAITS a
        # returned promise — handing back the promise itself would hang the test.
        "() => { renderSpecialtyPicker({ dismissable: true }); }"
    )
    page.wait_for_selector(".asc-sheet")
    page.wait_for_timeout(250)                       # let the fade-in settle
    after = page.screenshot(clip={"x": 0, "y": 0, "width": 400, "height": 50})
    assert before != after, "the scrim does not visibly dim the header behind it"
