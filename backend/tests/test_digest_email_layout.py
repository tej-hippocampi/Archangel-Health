"""The redesigned digest emails: list-free layout, no separator glyphs.

The digest body arrives from a model, so the converter has to hold its shape
against whatever punctuation the model leans on. The contract these tests pin:
no <ul> anywhere (tables render consistently across mail clients, lists do
not), no long dashes in the output, a bold lead phrase carrying each item, and
a personalized greeting.
"""

import re

import onboarding_emails as oe


def _news(body_markdown, **overrides):
    kwargs = dict(
        first_name="Amara",
        headline="Three things moved in medical AI",
        body_markdown=body_markdown,
        community_url="https://x/community",
        unsubscribe_url="https://x/unsub?token=t",
    )
    kwargs.update(overrides)
    return oe.build_community_news_digest_email(**kwargs)


# ─── The news digest ─────────────────────────────────────────────────────────

def test_news_digest_renders_no_html_lists():
    out = _news("- one story. with detail\n- two\n\nA plain paragraph.\n- three")
    assert "<ul" not in out and "<li" not in out


def test_news_digest_has_no_unbalanced_list_tags():
    """The old converter emitted a stray closing tag for any leading paragraph."""
    out = _news("A lead paragraph first.\n- then a bullet")
    assert "</ul>" not in out


def test_news_digest_strips_long_dashes_from_model_text():
    out = _news("- FDA cleared a model — the first of its kind\n– stray dash item")
    assert "—" not in out and "–" not in out


def test_news_digest_bolds_a_lead_phrase_per_item():
    out = _news("- FDA clears autonomous triage model. Sign-off moves to the pharmacist.")
    assert re.search(r"<strong[^>]*>FDA clears autonomous triage model\.</strong>", out)
    assert "Sign-off moves to the pharmacist." in out


def test_news_digest_accepts_unicode_bullets_and_headings():
    out = _news("## The headlines\n• a bulleted story with a unicode glyph")
    assert "THE HEADLINES" in out.upper()
    assert "a bulleted story" in out


def test_news_digest_greets_by_first_name_with_fallback():
    assert "Amara" in _news("- x")
    assert "there" in _news("- x", first_name="")


def test_news_digest_subject_is_the_headline_without_prefix():
    out = _news("- x", headline="Regulators moved on scribes")
    assert "<title>Regulators moved on scribes</title>" in out


def test_news_digest_still_escapes_markup():
    out = _news("- a story about <script>alert(1)</script> tooling",
                headline="<img src=x onerror=alert(1)>")
    assert "<script>" not in out and "<img" not in out
    assert "&lt;script&gt;" in out


# ─── The activity digest ─────────────────────────────────────────────────────

def test_activity_digest_takes_plain_items_and_escapes_them():
    out = oe.build_community_digest_email(
        activity_items=[("Dr. <b>X</b> mentioned you", "see the thread — soon")],
        community_url="https://x/community",
    )
    assert "<ul" not in out and "<li" not in out
    assert "&lt;b&gt;" in out
    assert "—" not in out


def test_activity_digest_subject_has_no_separator_glyphs():
    out = oe.build_community_digest_email(
        activity_items=[("A", "b")], community_url="https://x/community")
    m = re.search(r"<title>([^<]*)</title>", out)
    assert m and not re.search(r"[–—:]", m.group(1))


# ─── Whole-file posture ──────────────────────────────────────────────────────

def test_no_email_subject_carries_a_long_dash():
    """Subjects are the most-seen line of copy the product ships."""
    import inspect
    src = inspect.getsource(oe)
    for line in src.split("\n"):
        if "subject=" in line and not line.lstrip().startswith("#"):
            assert "—" not in line and "–" not in line, line
