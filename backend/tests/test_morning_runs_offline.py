"""The morning routine, demonstrable without buying a key.

It could not be run offline at all, and the failure was invisible: with no
`ANTHROPIC_API_KEY` every scope recorded a quiet day, which is the same thing
the routine records when the web genuinely had nothing. A developer had no way
to tell a working feature from a broken one, and the feature sat dark.

Three independent blockers, each sufficient on its own:

  1. `available("anthropic")` was False, and `_ask` short-circuits on it BEFORE
     the LLM client is consulted, so the fake transport was never reached;
  2. the fake intercepted any call carrying `tools`, but Anthropic's hosted web
     search is a SERVER-SIDE tool with no `input_schema` and the caller wants
     the model's text having searched, not a synthesized payload;
  3. both morning fixtures returned prose, which `_parse_items` reads as [].

This suite is the regression guard for all three, and for the one thing that
must NOT have changed with them: the citation gate.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from community import search_providers, websearch


@pytest.fixture(autouse=True)
def _offline_harness(monkeypatch):
    """Both switches, which is the point.

    The suite sets ASCLEPIUS_LLM_PROVIDER=fake for every run, so the harness
    keys on an EXPLICIT second variable: keying on the transport alone would
    have switched it on underneath the tests that verify the citation gate and
    the missing-key reason, and those are the tests that make the gate
    trustworthy."""
    monkeypatch.setenv("ASCLEPIUS_LLM_PROVIDER", "fake")
    monkeypatch.setenv("COMMUNITY_FAKE_SEARCH", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("COMMUNITY_SEARCH_PROVIDERS", "anthropic")


def test_the_offline_harness_needs_no_vendor_key():
    """The key gates a VENDOR. Under the harness there is no vendor to gate,
    and refusing here is what kept the whole routine unobservable."""
    assert search_providers.available("anthropic") is True


def test_a_key_is_still_required_for_the_real_transport(monkeypatch):
    monkeypatch.setenv("ASCLEPIUS_LLM_PROVIDER", "anthropic")
    assert search_providers.available("anthropic") is False


def test_the_harness_stays_off_until_it_is_asked_for(monkeypatch):
    """The fake transport ALONE must not switch it on: that is every test run
    in this repo, including the ones asserting a missing key is reported."""
    monkeypatch.delenv("COMMUNITY_FAKE_SEARCH", raising=False)
    assert search_providers.available("anthropic") is False


def test_every_morning_search_returns_items_offline():
    """All four, because each is a separate call site and a fixture registered
    for one purpose says nothing about the others."""
    for search in (websearch.search_events, websearch.search_news,
                   websearch.search_opportunities, websearch.search_new_research):
        items = asyncio.run(search(specialty="nephrology"))
        assert items, f"{search.__name__} sourced nothing under the fake transport"
        assert all(i.get("url") for i in items), search.__name__


def test_the_items_carry_the_caption_the_cards_render():
    """A brief of three titles and no sentences is a list of links. Every
    composer reads `summary` or `why` for the line under the title."""
    items = asyncio.run(websearch.search_events(specialty="nephrology"))
    assert all(i.get("why") or i.get("summary") for i in items)


def test_two_scopes_on_one_morning_do_not_collide():
    """The fixture returned identical URLs for every call, so the cross-channel
    dedupe correctly swallowed every scope after the first and the harness could
    only ever demonstrate one channel per run."""
    events = asyncio.run(websearch.search_events(specialty="nephrology"))
    news = asyncio.run(websearch.search_news(specialty="nephrology"))
    assert {i["url"] for i in events}.isdisjoint({i["url"] for i in news})


# ── What must not have moved ────────────────────────────────────────────────

def test_the_citation_gate_is_still_one_unconditional_rule():
    """The exemption is a named branch in `_ask`, NOT a relaxation of
    _keep_cited. Every real provider goes through the same gate, and a provider
    added later cannot inherit an exception it does not know about."""
    src = inspect.getsource(websearch._keep_cited)
    # The docstring is where this rule is ARGUED, so the assertion is about the
    # code under it.
    body = src[src.index('"""', src.index('"""') + 3) + 3:]
    for escape in ("fake", "ASCLEPIUS_LLM_PROVIDER", "getenv"):
        assert escape not in body, f"_keep_cited learned about {escape}"


def test_the_harness_substitutes_the_allowlist_and_never_skips_the_gate():
    """The first version of this SKIPPED _keep_cited, which also drops anything
    that is not http(s), so a `javascript:` URL sailed through the harness. It
    swaps where the allowlist comes from and nothing else."""
    src = inspect.getsource(websearch._ask)
    assert "_fake_search_enabled()" in src
    # The CALL, not the mentions of it in the comment above it.
    assert src.count("return _keep_cited(") == 1, "the gate is not the single exit"
    assert "return items" not in src, "there is a path around the gate"


def test_a_non_http_url_is_refused_even_in_the_harness():
    kept = websearch._keep_cited(
        [{"title": "Nope", "url": "javascript:alert(1)"}],
        {websearch._normalize("javascript:alert(1)")})
    assert kept == []


def test_the_events_query_asks_about_a_year_it_computes():
    """A hardcoded year does not fail loudly. It quietly stops matching, every
    scope records nothing_found, and the community looks like a quiet web
    rather than a stale prompt."""
    src = inspect.getsource(websearch.search_events)
    assert "2026" not in src
    assert "_search_years()" in src


def test_the_year_hint_reaches_forward_at_the_turn_of_the_year(monkeypatch):
    """The events window is the next sixty days, so a run in November has to
    ask about January or the routine goes quiet over exactly the weeks people
    plan next year's conferences."""
    import datetime as _dt

    class _Nov(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 15, tzinfo=tz)

    monkeypatch.setattr(websearch, "datetime", _Nov)
    assert websearch._search_years() == "2026 OR 2027"
