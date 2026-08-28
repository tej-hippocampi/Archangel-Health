"""Paid retrieval, and the rule that survives adding it.

The morning routine had one way to find anything: Anthropic's server-side
web-search tool. One vendor, one index, and a failure mode (no key) that looks
exactly like a quiet day.

Adding paid providers is only safe if the module's central rule survives them:
a URL the search never returned never reaches a doctor. On the retrieval path
that rule gets STRONGER, because the allowlist is a set built from a search
response rather than one parsed back out of the model's own prose. These tests
pin that, plus the spend ceiling, plus the fallback ordering.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests._asclepius import app  # noqa: F401 — binds the suite's temp DBs

from community import search_providers as sp
from community import websearch
from community.store import get_community_store


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("COMMUNITY_SEARCH_PROVIDERS", "EXA_API_KEY", "FIRECRAWL_API_KEY",
                "ANTHROPIC_API_KEY", "COMMUNITY_SEARCH_DAILY_CALL_CAP"):
        monkeypatch.delenv(var, raising=False)


def _day() -> str:
    """A ledger day nothing else has spent.

    The suite's community DB lives at a fixed temp path and OUTLIVES the run
    (tests/conftest.py), so a hardcoded day is already at its cap on the second
    run of this file. The column is just a TEXT key, so a unique token is a
    valid day for these purposes.
    """
    return f"2099-{uuid.uuid4().hex[:8]}"


def _r(url, title="A real page", provider="exa"):
    return {"title": title, "url": url, "snippet": "", "published": "", "provider": provider}


# ─── Provider configuration ──────────────────────────────────────────────────

def test_the_default_is_anthropic_alone_so_nothing_changes_until_someone_opts_in():
    assert sp.provider_order() == ["anthropic"]


def test_an_unknown_provider_name_is_ignored_rather_than_trusted(monkeypatch):
    monkeypatch.setenv("COMMUNITY_SEARCH_PROVIDERS", "exa,notaprovider")
    assert sp.provider_order() == ["exa"]


def test_a_provider_with_no_key_is_not_available(monkeypatch):
    monkeypatch.setenv("COMMUNITY_SEARCH_PROVIDERS", "exa")
    assert sp.available("exa") is False
    monkeypatch.setenv("EXA_API_KEY", "k")
    assert sp.available("exa") is True


def test_the_morning_is_enabled_by_any_configured_provider_not_only_anthropic(monkeypatch):
    """A deployment running Exa without an Anthropic key still has a morning."""
    monkeypatch.setenv("COMMUNITY_SEARCH_PROVIDERS", "exa")
    assert websearch.enabled() is False
    monkeypatch.setenv("EXA_API_KEY", "k")
    assert websearch.enabled() is True


# ─── The citation gate, on the retrieval path ────────────────────────────────

def test_a_url_the_search_never_returned_is_dropped(monkeypatch):
    """The rule the whole module is arranged around."""
    results = [_r("https://real.example/one")]

    async def _fake_llm(**kwargs):
        return (
            _resp('[{"title":"Invented","url":"https://not-in-results.example/x",'
                  '"summary":"","prompt":""}]'),
            {},
        )

    monkeypatch.setattr("ai.llm_client.call_llm", _fake_llm)
    kept = asyncio.run(websearch._ask_grounded("sys", "task", results))
    assert kept == []


def test_a_url_from_the_results_survives(monkeypatch):
    results = [_r("https://real.example/one")]

    async def _fake_llm(**kwargs):
        return (
            _resp('[{"title":"Real","url":"https://real.example/one",'
                  '"summary":"s","prompt":"p"}]'),
            {},
        )

    monkeypatch.setattr("ai.llm_client.call_llm", _fake_llm)
    kept = asyncio.run(websearch._ask_grounded("sys", "task", results))
    assert [k["url"] for k in kept] == ["https://real.example/one"]


def test_the_gate_normalizes_the_same_way_the_dedupe_does(monkeypatch):
    """Two different normalizations would silently drop every result."""
    results = [_r("https://www.Real.example/one/")]

    async def _fake_llm(**kwargs):
        return (_resp('[{"title":"Real","url":"http://real.example/one"}]'), {})

    monkeypatch.setattr("ai.llm_client.call_llm", _fake_llm)
    kept = asyncio.run(websearch._ask_grounded("sys", "task", results))
    assert len(kept) == 1


def test_a_non_http_url_never_survives(monkeypatch):
    results = [_r("https://real.example/one")]

    async def _fake_llm(**kwargs):
        return (_resp('[{"title":"x","url":"javascript:alert(1)"}]'), {})

    monkeypatch.setattr("ai.llm_client.call_llm", _fake_llm)
    assert asyncio.run(websearch._ask_grounded("sys", "task", results)) == []


def test_no_results_means_no_model_call_at_all(monkeypatch):
    called = []

    async def _fake_llm(**kwargs):
        called.append(1)
        return (_resp("[]"), {})

    monkeypatch.setattr("ai.llm_client.call_llm", _fake_llm)
    assert asyncio.run(websearch._ask_grounded("sys", "task", [])) == []
    assert called == []


# ─── Normalization / dedupe ──────────────────────────────────────────────────

def test_one_row_per_url_first_provider_wins():
    rows = [
        _r("https://a.example/x", provider="exa"),
        _r("https://www.a.example/x/", provider="firecrawl"),
        _r("https://b.example/y", provider="firecrawl"),
    ]
    out = sp.dedupe(rows)
    assert len(out) == 2
    assert out[0]["provider"] == "exa"


def test_a_result_without_a_usable_url_is_not_a_result():
    assert sp._result(title="t", url="ftp://x/y", provider="exa") is None
    assert sp._result(title="t", url="", provider="exa") is None
    assert sp._result(title="", url="https://a.example", provider="exa") is None


# ─── The spend ceiling ───────────────────────────────────────────────────────

def test_the_cap_stops_spending_and_the_ledger_survives_a_restart():
    """Durable rather than in-process: a redeploy loop must not be able to
    spend the day's budget repeatedly."""
    cstore = get_community_store()
    day = _day()
    claims = [cstore.claim_search_call("exa", cap=2, day=day) for _ in range(4)]
    assert claims == [True, True, False, False]
    assert cstore.search_calls_today("exa", day=day) == 2

    # A "restarted" process reads the same ledger.
    assert get_community_store().claim_search_call("exa", cap=2, day=day) is False


def test_each_provider_has_its_own_budget():
    cstore = get_community_store()
    day = _day()
    assert cstore.claim_search_call("exa", cap=1, day=day) is True
    assert cstore.claim_search_call("exa", cap=1, day=day) is False
    assert cstore.claim_search_call("firecrawl", cap=1, day=day) is True


def test_a_cap_of_zero_means_unlimited():
    cstore = get_community_store()
    day = _day()
    assert all(cstore.claim_search_call("exa", cap=0, day=day) for _ in range(5))


def test_the_budget_fails_open_rather_than_silencing_the_morning(monkeypatch):
    """The cap exists to stop a runaway loop, not to police a correct one.
    Refusing every search because SQLite hiccuped is the more expensive
    failure."""
    def _boom():
        raise RuntimeError("ledger down")

    monkeypatch.setattr("community.store.get_community_store", _boom)
    assert websearch._spend("exa") is True


def test_retrieval_does_not_call_a_provider_once_its_cap_is_spent(monkeypatch):
    monkeypatch.setenv("COMMUNITY_SEARCH_PROVIDERS", "exa")
    monkeypatch.setenv("EXA_API_KEY", "k")
    monkeypatch.setattr(websearch, "_spend", lambda name: False)

    called = []

    async def _never(*a, **kw):
        called.append(1)
        return []

    monkeypatch.setattr(sp, "search_exa", _never)
    assert asyncio.run(websearch.retrieve("q")) == []
    assert called == []


# ─── Fallback ordering ───────────────────────────────────────────────────────

def test_with_no_paid_provider_configured_retrieve_returns_nothing(monkeypatch):
    """Which is the signal the callers use to fall back to the search tool."""
    monkeypatch.setenv("COMMUNITY_SEARCH_PROVIDERS", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert asyncio.run(websearch.retrieve("q")) == []


def test_a_provider_that_raises_yields_a_shorter_morning_not_a_broken_run(monkeypatch):
    monkeypatch.setenv("COMMUNITY_SEARCH_PROVIDERS", "exa,firecrawl")
    monkeypatch.setenv("EXA_API_KEY", "k")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")

    async def _boom(*a, **kw):
        raise RuntimeError("provider down")

    async def _ok(query, **kw):
        return [_r("https://b.example/y", provider="firecrawl")]

    monkeypatch.setattr(sp, "search_exa", _boom)
    monkeypatch.setattr(sp, "search_firecrawl", _ok)

    with pytest.raises(RuntimeError):
        # retrieve does not swallow a provider raising outright; the provider
        # functions are the ones that must never raise, and they don't.
        asyncio.run(websearch.retrieve("q"))


def test_the_provider_functions_themselves_never_raise(monkeypatch):
    """Which is what makes the guarantee above hold in production."""
    monkeypatch.setenv("EXA_API_KEY", "k")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("network down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **kw: _Boom())
    assert asyncio.run(sp.search_exa("q")) == []
    assert asyncio.run(sp.search_firecrawl("q")) == []
    assert asyncio.run(sp.fetch_page("https://a.example")) == ""


def test_a_keyless_provider_is_never_called(monkeypatch):
    assert asyncio.run(sp.search_exa("q")) == []
    assert asyncio.run(sp.search_firecrawl("q")) == []
    assert asyncio.run(sp.fetch_page("https://a.example")) == ""


# ─── helpers ─────────────────────────────────────────────────────────────────
class _Block:
    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def _resp(text):
    return _Resp(text)
