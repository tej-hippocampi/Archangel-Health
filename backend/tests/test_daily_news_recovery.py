"""Regression coverage for missed daily news and the silently green backup."""
import asyncio
import copy
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest

from community import digest, feeds, store as stores
from scripts import run_community_schedule as cron


class Clock(datetime):
    current = datetime(2026, 9, 10, 13, 5)

    @classmethod
    def utcnow(cls):
        return cls.current


@pytest.fixture
def world(tmp_path, monkeypatch):
    from community import router
    monkeypatch.setattr(stores, "_stores", dict(stores._stores))
    db = stores.reset_community_store_for_tests(db_path=str(tmp_path / "news.db"))
    db.ensure_default_channels()
    monkeypatch.setattr(router, "member_map", lambda **kw: {})
    monkeypatch.setattr(Clock, "current", datetime(2026, 9, 10, 13, 5))
    monkeypatch.setattr(stores, "datetime", Clock)
    monkeypatch.setattr(digest, "datetime", Clock)
    monkeypatch.setenv("COMMUNITY_DIGEST_NEWS_HOUR_UTC", "13")
    monkeypatch.setenv("COMMUNITY_NEWS_ENABLED", "1")
    candidates = [feeds._item("rss:test", url=f"https://example.org/news/{i}",
                              title=f"Clinical AI evaluation improves {i}") for i in range(3)]
    drafts = []
    calls = []
    valid = {"items": [{"headline": it["title"], "deck": "Researchers measured diagnostic accuracy.",
                        "why_it_matters": "You can compare clinical model performance.",
                        "url": it["url"], "source": "Test Wire", "section": "Research"}
                       for it in candidates]}

    async def fetch(kind):
        return list(candidates)

    async def llm(*, messages, **kwargs):
        sent = json.loads(messages[0]["content"])
        if "digest_kind" not in sent:
            return json.dumps({"items": [{"id": it["id"], "keep": True,
                "relevance": 0.9, "one_liner": "Researchers measured accuracy."}
                for it in sent["items"]]}), {}
        calls.append(copy.deepcopy(messages))
        payload = drafts.pop(0) if drafts else valid
        return payload if isinstance(payload, str) else json.dumps(payload), {}

    monkeypatch.setattr(digest, "_fetch", fetch)
    monkeypatch.setattr("ai.llm_client.call_llm", llm)
    monkeypatch.setattr("ai.llm_client.first_text", lambda value: value)
    return SimpleNamespace(db=db, candidates=candidates, drafts=drafts, calls=calls, valid=valid)


def tick():
    return asyncio.run(digest.run_scheduled_digest("news"))


@pytest.mark.parametrize("failure", ["word_limit", "invalid_json", "invented_url", "duplicate_url"])
def test_invalid_draft_is_corrected_before_one_post(world, failure):
    broken = copy.deepcopy(world.valid)
    if failure == "word_limit":
        broken["items"][0]["headline"] = " ".join(["word"] * 11)
    elif failure == "invalid_json":
        broken = "incomplete JSON"
    elif failure == "invented_url":
        broken["items"][0]["url"] = "https://invented.example/story"
    else:
        broken["items"][1]["url"] = broken["items"][0]["url"] + "/"
    world.drafts.append(broken)
    result = tick()
    assert result["ok"] and result["posted"] == 3
    assert len(world.calls) == 2
    assert "Validation failed:" in world.calls[1][-1]["content"]
    channel = world.db.get_channel_by_slug("medical-ai-news")
    assert len(world.db.list_messages(channel["id"])[0]) == 1
    assert tick()["outcome"] == "not_due"
    assert len(world.calls) == 2


def test_repeated_invalid_drafts_stop_then_retry_without_losing_sources(world):
    broken = copy.deepcopy(world.valid)
    broken["items"][0]["headline"] = " ".join(["word"] * 11)
    world.drafts.extend([broken] * 3)
    assert tick()["ok"] is False
    assert len(world.calls) == 3
    assert len(world.db.new_content_items()) == 3
    assert tick()["outcome"] == "backing_off"
    Clock.current += timedelta(hours=2)
    assert tick()["posted"] == 3


def test_empty_scheduled_run_does_not_consume_the_day(world):
    saved = list(world.candidates)
    world.candidates.clear()
    result = tick()
    assert result["ok"] is False and result["reason"] == "no_source_items"
    assert tick()["outcome"] == "backing_off"
    world.candidates.extend(saved)
    Clock.current += timedelta(hours=2)
    assert tick()["posted"] == 3
    assert tick()["outcome"] == "not_due"


def test_two_stories_can_be_joined_by_a_third_later_today(world):
    third = world.candidates.pop()
    assert tick()["reason"] == "below_item_floor"
    assert len(world.db.new_content_items()) == 2
    world.candidates.append(third)
    Clock.current += timedelta(hours=2)
    assert tick()["posted"] == 3


def test_legacy_empty_success_is_reclaimed_but_published_and_active_are_not(world):
    run = world.db.claim_digest_run("news", window_key="2026-09-10")
    world.db.finish_digest_run(run, ok=True, items_posted=0, reason="nothing_new")
    assert tick()["posted"] == 3
    reopened = stores.CommunityStore(db_path=world.db.db_path)
    assert reopened.claim_digest_run("news", window_key="2026-09-10", require_posted=True) is None
    assert reopened.claim_digest_run("news", window_key="2026-09-11", require_posted=True)
    assert reopened.claim_digest_run("news", window_key="2026-09-11", require_posted=True) is None


ENV = {"MORNING_BASE_URL": "https://app.example.test", "INTERNAL_TOOL_SECRET": "test-only-secret"}


class Responses:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


@pytest.mark.parametrize("env", [{}, {"MORNING_BASE_URL": ENV["MORNING_BASE_URL"]},
                                 {"INTERNAL_TOOL_SECRET": "test-only-secret"}])
def test_missing_configuration_fails_without_a_request(env, capsys):
    net = Responses()
    assert cron.run("news", environ=env, opener=net) == 1
    assert not net.requests
    assert "::error::" in capsys.readouterr().out


@pytest.mark.parametrize("result,exit_code", [
    ({"ok": True, "posted": 3}, 0),
    ({"ok": True, "outcome": "not_due", "posted": 0}, 0),
    ({"ok": True, "outcome": "already_running", "posted": 0}, 0),
    ({"ok": False, "error": "bad compose"}, 1),
    ({"ok": True, "posted": 0, "reason": "nothing_new"}, 1),
    ({"ok": True, "outcome": "backing_off", "posted": 0}, 1),
    ({}, 1), ([], 1), ({"ok": True}, 1), ({"ok": True, "posted": "3"}, 1),
])
def test_http_success_must_also_be_application_success(result, exit_code):
    net = Responses(result)
    assert cron.run("news", environ=ENV, opener=net) == exit_code
    assert net.requests[0].full_url.endswith("kind=news&scheduled=true")


def test_failed_morning_does_not_skip_other_routines_or_leak_errors(capsys):
    net = Responses(urllib.error.URLError("test-only-secret"), {"cohorts": [], "sent": 0},
                    {"ok": True, "outcome": "not_due"}, {"ok": True}, {"ok": True})
    assert cron.run("routine", environ=ENV, opener=net) == 1
    assert len(net.requests) == 5
    assert "test-only-secret" not in capsys.readouterr().out


def test_backup_runs_news_before_other_routines_even_when_news_fails():
    import yaml
    workflow = yaml.safe_load((Path(__file__).parents[2] / ".github/workflows/community-morning.yml").read_text())
    jobs = workflow["jobs"]
    assert "needs" not in jobs["news"]
    assert jobs["routine"]["needs"] == "news"
    assert "!cancelled()" in jobs["routine"]["if"]
    assert jobs["news"]["steps"][-1]["run"].endswith("run_community_schedule.py news")
