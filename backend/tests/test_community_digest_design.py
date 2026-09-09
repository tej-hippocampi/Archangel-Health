"""The digest card, the greeting and the empty states, RENDERED (Digest Design PRD §6).

Source-grepping a frontend module proves it was written, not that it works, and
this repo has already paid for that lesson: a section can be complete, correct
and invisible for a whole build round because nothing mounted it. So the render
assertions here execute the shipped functions from ``community.js`` against the
DOM shim and look at what lands in the tree.

The one thing a shim cannot check is the cascade, so the CSS assertions at the
bottom are deliberately structural — the classes the card emits exist, the file
still balances, and the serif that makes the title a title is declared HERE
rather than borrowed from a stylesheet this page does not load.
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
_DOM_SHIM = Path(__file__).resolve().parent / "_asclepius_dom.js"
_JS = (_FRONTEND / "community.js").read_text(encoding="utf-8")
_CSS = (_FRONTEND / "community.css").read_text(encoding="utf-8")
_HTML = (_FRONTEND / "community.html").read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f"node failed:\n{proc.stderr}\n{proc.stdout}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _extract(name: str, kind: str = "function") -> str:
    """One declaration, verbatim from the shipped file.

    Verbatim is the whole point: a harness that re-types the render function is
    a harness that tests the re-typing. Nesting is counted rather than matched
    by regex because every one of these bodies contains braces, brackets and
    quotes, and a `};` inside a string literal is not the end of anything.
    """
    needle = f"function {name}(" if kind == "function" else f"const {name} = "
    start = _JS.index(needle)
    depth, quote, i = 0, "", _JS.index("(" if kind == "function" else "=", start)
    while i < len(_JS):
        ch = _JS[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
            # A function ends at its closing brace; a const runs on to the
            # semicolon, which may be several characters later.
            if depth == 0 and kind == "function" and ch == "}":
                return _JS[start:i + 1]
        elif ch == ";" and depth == 0 and kind != "function":
            return _JS[start:i + 1]
        i += 1
    raise AssertionError(f"never closed while extracting {name} from community.js")


_HARNESS = r"""
require(%(shim)s);
const state = { preview: false, canPost: true, me: {user_id:'u-me', display_name:'Kalpesh Patel'},
                members: [], online: new Set(), channels: [], active: 'general',
                latestDigest: null };
%(h)s
%(append)s
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
const opened = [];
function openThread(id) { opened.push(['thread', id]); }
function openChannel(slug) { opened.push(['channel', slug]); }
// The shim gives every ELEMENT a querySelector but not the document. A browser
// has both, and `openDigestPost` scrolls to a row through the document one.
document.querySelector = (sel) => document.body.querySelector(sel);
%(code)s
%(body)s
"""


def _render(body: str, funcs, consts=()) -> dict:
    code = "\n".join([_extract(c, "const") for c in consts]
                     + [_extract(f) for f in funcs])
    return _run_node(_HARNESS % {
        "shim": json.dumps(str(_DOM_SHIM)),
        "h": _extract("h"),
        "append": _extract("append"),
        "code": code,
        "body": body,
    })


_CARD_FUNCS = ("svgIcon", "tagChipEl", "digestOf", "digestLead", "digestSourceEl",
               "digestHeadlineEl", "digestLeadEl", "digestItemEl", "digestWeekday",
               "digestCardEl")
_CARD_CONSTS = ("SVG_NS", "DIGEST_TAGS")

_PAYLOAD = {
    "title": "Medical AI Digest",
    "items": [
        {"headline": "Regulators open closed-door talks on clinical AI",
         "deck": "The rules for approved clinical AI are being written now.",
         "why_it_matters": "You will practise under them.",
         "source": "STAT", "url": "https://example.org/a",
         "section": "Regulation", "urgent": False, "lead": True},
        {"headline": "Drug-diversion AI works only when staff use it",
         "deck": "", "why_it_matters": "A monitoring tool is a policy.",
         "source": "Modern Healthcare", "url": "https://example.org/b",
         "section": "Deployment", "urgent": False, "lead": False},
        {"headline": "ED trial: AI did not beat clinicians",
         "deck": "", "why_it_matters": "Loudest claims, thinnest evidence.",
         "source": "Annals", "url": "https://example.org/c",
         "section": "Evals", "urgent": False, "lead": False},
    ],
}


def _msg(**over):
    payload = json.loads(json.dumps(_PAYLOAD))
    payload.update(over.pop("payload", {}))
    return dict({"id": 42, "kind": "digest_news", "payload": payload,
                 "created_at": "2026-09-08T13:00:00Z", "reply_count": 0}, **over)


def _card(msg=None, collapsed=False) -> dict:
    return _render(
        """
        const m = %(msg)s;
        const card = digestCardEl(m, digestOf(m), { collapsed: %(collapsed)s });
        const cls = (n) => (n.className || '').split(/\\s+/).filter(Boolean);
        const walk = (n, out) => {
          if (n.classList) out.push({ cls: cls(n), tag: n.tagName,
                                      text: n.textContent, href: n.getAttribute && n.getAttribute('href') });
          (n.childNodes || []).forEach((c) => walk(c, out));
          return out;
        };
        console.log(JSON.stringify({ nodes: walk(card, []), text: card.textContent }));
        """ % {"msg": json.dumps(msg or _msg()),
               "collapsed": "true" if collapsed else "false"},
        _CARD_FUNCS, _CARD_CONSTS)


def _classes(res, name):
    return [n for n in res["nodes"] if name in n["cls"]]


# ═══ §6 render ═══════════════════════════════════════════════════════════════

def test_a_structured_payload_renders_a_card_and_a_legacy_body_does_not():
    """The migration in one assertion: `digestOf` is the whole switch, and a
    post written before the payload column existed must fall through to
    `renderBody` rather than render a card with holes in it."""
    res = _render(
        """
        const withPayload = %(m)s;
        const legacy = { id: 9, kind: 'digest_news', body: 'old markdown', created_at: 'x' };
        const chatter = { id: 10, kind: 'message', payload: withPayload.payload };
        console.log(JSON.stringify({
          structured: !!digestOf(withPayload),
          legacy: !!digestOf(legacy),
          wrongKind: !!digestOf(chatter),
          holed: !!digestOf({ id: 11, kind: 'digest_news', payload: { items: [{}] } }),
        }));
        """ % {"m": json.dumps(_msg())},
        ("digestOf",))
    assert res == {"structured": True, "legacy": False,
                   "wrongKind": False, "holed": False}


def test_the_tag_class_matches_the_section_on_every_item():
    """Five sections, five chips, and the class is what carries the tint. A
    chip whose class does not follow its section is an amber Research story."""
    res = _card()
    chips = _classes(res, "cm-tag")
    assert len(chips) == 3
    got = {c["text"]: [x for x in c["cls"] if x.startswith("cm-tag-")] for c in chips}
    assert got == {"Regulation": ["cm-tag-regulation"],
                   "Deployment": ["cm-tag-deployment"],
                   "Evals": ["cm-tag-evals"]}


def test_the_five_tag_icons_are_inline_svg_and_the_page_loads_no_icon_font():
    """§4: five glyphs are not worth a font, and `document.createElement('svg')`
    silently produces an element that paints nothing — so the namespace call is
    the assertion, not the markup."""
    assert "createElementNS(SVG_NS, 'svg')" in _JS
    assert "createElementNS(SVG_NS, 'path')" in _JS
    for font in ("tabler-icons", "fonts.googleapis", "@font-face", "icon-font"):
        assert font not in _HTML, f"the community page started loading {font}"
    res = _card()
    assert len(_classes(res, "cm-tag")) == 3
    svgs = [n for n in res["nodes"] if n["tag"] == "SVG"]
    assert len(svgs) == 3, "a tag chip rendered without its glyph"


def test_the_lead_carries_a_rule_and_a_badge_and_the_others_do_not():
    res = _card()
    leads = _classes(res, "cm-digest-lead")
    assert len(leads) == 1
    # The rule's colour comes off the tag class on the lead itself.
    assert "cm-tag-regulation" in leads[0]["cls"]
    badges = _classes(res, "cm-digest-badge")
    assert [b["text"] for b in badges] == ["TOP STORY"]
    assert len(_classes(res, "cm-digest-item")) == 2


def test_breaking_replaces_top_story_only_when_the_lead_says_urgent():
    """A badge that fires by default is a badge that has stopped meaning
    anything by the second week."""
    msg = _msg()
    msg["payload"]["items"][0]["urgent"] = True
    assert [b["text"] for b in _classes(_card(msg), "cm-digest-badge")] == ["BREAKING"]
    # Urgency on a story that is not the lead never reaches the badge.
    msg2 = _msg()
    msg2["payload"]["items"][2]["urgent"] = True
    assert [b["text"] for b in _classes(_card(msg2), "cm-digest-badge")] == ["TOP STORY"]


def test_only_the_lead_carries_a_deck_and_a_lime_wash():
    """§0.5: the compact item is a headline and one line. A deck under it is the
    third line the design deleted, and a second wash makes the first one mean
    nothing."""
    res = _card()
    assert len(_classes(res, "cm-digest-deck")) == 1
    assert len(_classes(res, "cm-why")) == 1
    # And no label above the wash — the wash is the label.
    assert "WHY IT MATTERS" not in res["text"].upper()


def test_every_item_says_full_article_and_names_its_publisher():
    """Never a bare host string: "statnews.com" is not how a person says STAT,
    and the link already carries the host."""
    res = _card()
    links = _classes(res, "cm-digest-link")
    assert len(links) == 3
    assert all(l["text"].startswith("Full article") for l in links)
    assert [l["href"] for l in links] == [
        "https://example.org/a", "https://example.org/b", "https://example.org/c"]
    assert sorted(p["text"] for p in _classes(res, "cm-digest-pub")) == [
        "Annals", "Modern Healthcare", "STAT"]


def test_a_url_that_is_not_http_renders_as_text_rather_than_a_dead_link():
    msg = _msg()
    msg["payload"]["items"][1]["url"] = "javascript:alert(1)"
    res = _card(msg)
    assert len(_classes(res, "cm-digest-link")) == 2
    assert all("javascript:" not in (n["href"] or "") for n in res["nodes"])


def test_the_title_is_three_words_with_a_weekday_and_a_count_beside_it():
    res = _card()
    assert [t["text"] for t in _classes(res, "cm-digest-title")] == ["Medical AI Digest"]
    meta = _classes(res, "cm-digest-meta")[0]["text"]
    assert meta.endswith("3 stories") and "·" in meta
    # Nothing under the title.
    assert "cm-digest-deck" not in _classes(res, "cm-digest-head")[0]["cls"]


def test_the_footer_is_one_word_and_an_arrow():
    foot = _classes(_card(), "cm-digest-foot")
    assert len(foot) == 1 and foot[0]["text"] == "Discuss→"


# ═══ §6 home — the pinned card ═══════════════════════════════════════════════

def test_the_pinned_card_is_the_same_component_collapsed():
    """§1.3: one component, two contexts. Collapsed shows the title and the lead
    and offers the way in; a second component is a second place to drift."""
    res = _card(collapsed=True)
    assert len(_classes(res, "cm-digest-lead")) == 1
    assert len(_classes(res, "cm-digest-item")) == 0, "a compact item leaked into the pinned card"
    foot = _classes(res, "cm-digest-foot")[0]
    assert foot["text"] == "Read all 3→"
    assert "cm-digest-pinned" in _classes(res, "cm-digest")[0]["cls"]


def test_the_pinned_card_renders_only_on_the_landing_room_and_never_in_preview():
    res = _render(
        """
        const m = %(m)s;
        const seen = {};
        for (const [slug, prev, has] of [['general', false, true], ['general', true, true],
                                         ['questions-help', false, true], ['general', false, false]]) {
          state.active = slug; state.preview = prev;
          state.latestDigest = has ? m : null;
          seen[slug + '|' + prev + '|' + has] = !!pinnedDigestEl();
        }
        console.log(JSON.stringify(seen));
        """ % {"m": json.dumps(_msg())},
        _CARD_FUNCS + ("pinnedDigestEl",), _CARD_CONSTS)
    assert res == {"general|False|True".replace("False", "false").replace("True", "true"): True,
                   "general|true|true": False,
                   "questions-help|false|true": False,
                   "general|false|false": False}


def test_read_all_opens_the_channel_post_rather_than_a_copy_of_it():
    """The card the reader taps and the card they land on are the same post, so
    the pinned one never has to be kept in sync with anything."""
    res = _render(
        """
        const m = %(m)s;
        const card = digestCardEl(m, digestOf(m), { collapsed: true });
        const foot = card.querySelector('.cm-digest-foot');
        foot.dispatch('click');
        console.log(JSON.stringify({ opened }));
        """ % {"m": json.dumps(_msg())},
        _CARD_FUNCS + ("openDigestPost",), _CARD_CONSTS)
    assert res["opened"] == [["channel", "medical-ai-news"]]


# ═══ §6 greeting ═════════════════════════════════════════════════════════════

def test_the_greeting_is_one_line_inside_its_budget():
    res = _render(
        """
        const out = {};
        for (const hour of [8, 14, 21]) {
          const d = new Date(2026, 8, 8, hour, 0, 0);
          out[hour] = greetingWord(d) + ', ' + greetingName(state.me) + '.';
        }
        out.already = greetingName({ display_name: 'Dr. Ali Rahman' });
        out.nameless = greetingName({ display_name: '   ' });
        console.log(JSON.stringify(out));
        """, ("greetingWord", "greetingName"))
    assert res["8"] == "Good morning, Dr. Patel."
    assert res["14"].startswith("Good afternoon") and res["21"].startswith("Good evening")
    # §0.5: nine words is the cap, and the longest of these is four.
    for hour in ("8", "14", "21"):
        assert len(res[hour].split()) <= 9, res[hour]
    assert res["already"] == "Dr. Ali Rahman", "a title was doubled up"
    assert res["nameless"] is None


def test_the_presence_counts_come_from_state_and_zero_online_is_a_hollow_dot():
    res = _render(
        """
        const out = {};
        document.body.appendChild(h('div', { id: 'cmGreet' }));
        const bar = document.getElementById('cmGreet');
        const snap = () => {
          renderGreeting();
          const dot = bar.querySelector('.cm-presence-dot');
          const digest = bar.querySelector('.cm-presence-digest');
          return { text: bar.textContent, on: dot ? dot.classList.contains('on') : null,
                   digest: digest ? digest.textContent : null };
        };
        state.members = [{ user_id: 'a' }, { user_id: 'b' }, { user_id: 'c' }];
        state.channels = [{ slug: 'medical-ai-news', unread: 1 }];
        state.online = new Set(['a', 'b', 'c']);
        out.three = snap();
        state.online = new Set();
        state.channels = [{ slug: 'medical-ai-news', unread: 0 }];
        out.zero = snap();
        state.preview = true;
        out.preview = snap();
        console.log(JSON.stringify(out));
        """, ("greetingWord", "greetingName", "renderGreeting"))
    assert res["three"]["on"] is True
    assert "3 online" in res["three"]["text"]
    assert res["three"]["digest"] == "1 new digest"
    assert res["zero"]["on"] is False, "nobody online and the dot is still filled"
    assert "0 online" in res["zero"]["text"]
    assert res["zero"]["digest"] is None, "a zero count rendered as a sentence"
    assert res["preview"]["text"] == "", "the preview greeted a fixture by name"


# ═══ §6 profile ══════════════════════════════════════════════════════════════

def test_the_profile_card_has_three_stats_and_no_money_on_it():
    res = _render(
        """
        function countryLabel(c) { return c === 'GB' ? 'United Kingdom' : c; }
        const m = { user_id: 'u-1', display_name: 'Dr. Amara Okafor', specialty: 'Nephrology',
                    years_in_practice: 17, country: 'GB' };
        const stats = profileStats(m);
        const vals = stats.querySelectorAll('.cm-profile-stat-v').map((n) => n.textContent);
        const keys = stats.querySelectorAll('.cm-profile-stat-k').map((n) => n.textContent);
        console.log(JSON.stringify({ vals, keys, cred: credentialLine(m),
                                     staff: credentialLine({ is_staff: true, years_in_practice: 3 }),
                                     bot: credentialLine({ is_bot: true }) }));
        """, ("profileStats", "credentialLine"))
    assert res["keys"] == ["Specialty", "In practice", "Country"]
    assert res["vals"] == ["Nephrology", "17 yrs", "United Kingdom"]
    assert res["cred"] == "Nephrology · 17 yrs"
    assert res["staff"] == "Archangel Health · 3 yrs"
    assert res["bot"] == "Archangel Health"
    # §3.2: no blurb, no earnings, no tier — on the card or anywhere near it.
    panel = _JS.split("function profileStats")[1][:1600].lower()
    for banned in ("earnings", "contributor_score", "tier", "payout", "blurb"):
        assert banned not in panel, f"{banned} is back on the profile card"


def test_the_founders_link_arrives_through_the_shell_and_never_from_the_portal():
    """§8.3. `FOUNDER_CALENDLY` lives in asclepius.js, which this page does not
    load. A second hardcoded copy is right until the first time it changes."""
    assert "data-founder-calendly" in _HTML
    assert "calendly" in _HTML
    # The community bundle holds no URL of its own and no second copy of the
    # portal's constant. (asclepius.js is named in a comment or two, which is a
    # citation; what must not exist is the value.)
    body = _JS.split("function founderCalendly")[1][:400]
    assert "calendly.com" not in body
    assert "calendly.com" not in _JS, "a second hardcoded copy of the founders' link"
    assert "FOUNDER_CALENDLY" not in _JS
    # Loaded, not merely mentioned: both files name asclepius.js in a comment
    # saying why they do not load it, and a substring grep cannot tell the
    # explanation from the thing it forbids.
    assert not re.search(r"""src=["'][^"']*asclepius\.js""", _HTML), \
        "the community page started loading the portal bundle"
    res = _render(
        """
        const out = {};
        out.none = founderCalendly();
        document.body.setAttribute('data-founder-calendly', 'https://calendly.com/x/y');
        out.set = founderCalendly();
        document.body.setAttribute('data-founder-calendly', 'javascript:alert(1)');
        out.hostile = founderCalendly();
        console.log(JSON.stringify(out));
        """, ("founderCalendly",))
    assert res == {"none": None, "set": "https://calendly.com/x/y", "hostile": None}


# ═══ §6 empty ════════════════════════════════════════════════════════════════

def test_every_empty_state_is_one_short_sentence_and_the_read_only_rooms_have_no_button():
    res = _render(
        """
        console.log(JSON.stringify({ copy: EMPTY_COPY, readOnly: READ_ONLY_ROOMS }));
        """, (), ("EMPTY_COPY", "READ_ONLY_ROOMS"))
    copy, read_only = res["copy"], res["readOnly"]
    assert set(copy) >= {"general", "introductions", "questions-help",
                         "future-of-medical-ai", "medical-ai-news", "task-announcements"}
    for slug, (sentence, button) in copy.items():
        assert len(sentence.split()) <= 12, f"#{slug}: {sentence}"
        if button is None:
            assert slug in read_only, f"#{slug} lost its button without being read-only"
        else:
            assert len(button.split()) <= 3, f"#{slug}: {button}"
    assert copy["general"][0] == "The kitchen table. Say hello."
    assert copy["questions-help"] == ["Ask. One of us answers today.", "Ask"]
    for slug in read_only:
        assert copy[slug][1] is None, f"#{slug} offers a post nobody may make"


def test_the_phi_footer_is_not_warmed_up_with_the_rest():
    """§3.3 in its own words: it is a rule, and it should not be warm."""
    assert "Do not post patient-identifiable information." in _JS
    assert _JS.count("cm-phi-notice") >= 2


# ═══ §6 css ══════════════════════════════════════════════════════════════════

def test_the_stylesheet_balances_and_declares_every_class_the_card_emits():
    assert _CSS.count("{") == _CSS.count("}"), "community.css braces do not balance"
    for cls in ("cm-digest", "cm-digest-title", "cm-digest-lead", "cm-digest-item",
                "cm-tag", "cm-tag-regulation", "cm-tag-research", "cm-tag-deployment",
                "cm-tag-evals", "cm-tag-opinion", "cm-why", "cm-greet",
                "cm-presence-dot", "cm-profile-stats", "cm-home-cta"):
        assert f".{cls}" in _CSS, f"{cls} is emitted by community.js and styled nowhere"


def test_the_serif_is_declared_here_and_asclepius_css_is_never_imported():
    """§1.1. Reusing `.asc-fr-letter-title` across bundles, or importing the
    portal stylesheet for one font stack, is how two pages start sharing rules
    neither of them owns."""
    block = _CSS[_CSS.index(".cm-digest-title"):][:400]
    assert "Georgia" in block and "serif" in block
    # No @import of the portal stylesheet, and no <link> to it. Both files name
    # it in the comment explaining why, so the check is on the loading form.
    assert "@import" not in _CSS
    assert not re.search(r"""href=["'][^"']*asclepius\.css""", _HTML)
    # And the portal's own title class is never borrowed. Only a SELECTOR would
    # do that; the prose above the rule cites it by name on purpose.
    assert ".asc-fr-letter-title" not in _CSS
    assert "asc-fr-letter-title" not in _JS
    # Serif on the digest title and nowhere else in this file. Counted over
    # DECLARATIONS, so the prose above the rule explaining why does not count.
    decls = [ln for ln in _CSS.splitlines()
             if "serif" in ln and "font-family" in ln]
    assert len(decls) == 1, decls


def test_the_tag_tints_are_washes_of_existing_tokens_and_add_no_new_hue():
    """§4 / §8.2: _tokens.css is read, never edited, and a colour this file
    needs is derived from a token rather than typed in as a fifth brand hex."""
    block = _CSS[_CSS.index(".cm-tag-regulation"):]
    block = block[:block.index("\n\n")] if "\n\n" in block else block
    assert re.search(r"#[0-9a-fA-F]{3,6}", block) is None, block
    for token in ("--orange-wash", "--lime-wash", "--green-wash", "--pink-wash"):
        assert token in block
