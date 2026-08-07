"""A shipped frontend change must actually reach the browser.

Starlette's ``StaticFiles`` sends ``ETag`` and ``Last-Modified`` but no
``Cache-Control``. With neither ``Cache-Control`` nor ``Expires``, a browser is
free to apply HEURISTIC freshness (RFC 9111 §4.2.2) — commonly a tenth of the
file's age since ``Last-Modified`` — and for that whole window it serves the
cached copy WITHOUT revalidating. Nothing errors. Nothing appears in the logs.
The deploy simply does not exist for anyone who loaded the page before it.

This is not hypothetical. It cost a real round trip: a header fix and a
case-panel rebuild were both merged and live, and the reported symptom was that
neither had changed — because the browser never asked the server for the new CSS.

The app has no build step, so there are no content-hashed filenames that could
be cached forever instead. That makes the trade one-directional: markup and code
must revalidate. The cost is a conditional request that answers 304 with an
empty body; the alternative is invisible deploys.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Repo convention (see test_asclepius_visual.py and siblings): put ``backend/``
# on the path before importing project modules, so this passes under both
# ``python -m pytest`` and the console-script entry point CI uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient                     # noqa: PLC0415
    from main import app                                          # noqa: PLC0415

    with TestClient(app) as c:
        yield c


# Files whose whole purpose is to change when we ship.
CODE_ASSETS = [
    "/static/asclepius/asclepius.css",
    "/static/asclepius/asclepius.js",
    "/static/asclepius/index.html",
    "/static/asclepius/manual-content.js",
]


@pytest.mark.parametrize("path", CODE_ASSETS)
def test_code_assets_must_be_revalidated_before_reuse(client, path):
    res = client.get(path)
    assert res.status_code == 200, f"{path} is not served at all"
    cc = res.headers.get("cache-control", "")
    assert cc, (
        f"{path} carries no Cache-Control, so the browser falls back to "
        "heuristic freshness and may serve a stale copy without ever asking. "
        "That is an invisible deploy."
    )
    assert "no-cache" in cc, f"{path} may be reused without revalidating: {cc!r}"


@pytest.mark.parametrize("path", CODE_ASSETS)
def test_revalidation_is_cheap_because_the_etag_still_answers(client, path):
    """``no-cache`` means "ask first", not "send it again every time".

    If the ETag stopped round-tripping, every page load would re-download the
    full payload and the honest fix would read as a performance regression.
    """
    first = client.get(path)
    etag = first.headers.get("etag")
    assert etag, f"{path} has no ETag, so revalidation cannot answer 304"
    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304, (
        f"{path} re-sent {len(again.content)} bytes instead of answering 304"
    )
    assert not again.content


def test_the_policy_is_not_blanket_no_cache(client):
    """Only code revalidates. Anything else keeps a long cache.

    A blanket ``no-cache`` would be the lazy version of this fix and would tax
    every font and image on every navigation for no benefit — those change only
    when their filename does.
    """
    import main                                                   # noqa: PLC0415

    static = main._RevalidatingStatic
    for name in ("app.js", "styles.css", "page.html", "data.json", "bundle.map"):
        assert name.lower().endswith(static.REVALIDATE), f"{name} should revalidate"
    for name in ("Font.woff2", "logo.png", "icon.svg", "photo.jpg"):
        assert not name.lower().endswith(static.REVALIDATE), (
            f"{name} should keep a long cache, not revalidate on every load"
        )


def test_the_guard_can_see_the_defect_it_exists_to_catch(client):
    """Mutation check. If ``_RevalidatingStatic`` were reverted to a plain
    ``StaticFiles``, the assertions above must fail — otherwise they are
    decoration. This asserts the header is absent from the UNPATCHED class, so
    the guard is pinned to the patch rather than to Starlette's defaults.
    """
    from fastapi.staticfiles import StaticFiles                    # noqa: PLC0415

    import main                                                    # noqa: PLC0415

    assert issubclass(main._RevalidatingStatic, StaticFiles)
    assert "file_response" in vars(main._RevalidatingStatic), (
        "the Cache-Control override is gone — plain StaticFiles sends no "
        "Cache-Control at all, which is the defect this file guards"
    )
