"""Every API path `frontend/doctor.html` calls is either served or visibly retired.

`doctor.html` is the legacy peri-op dashboard. It is kept — `doctor-sign-in.html`
redirects to `/doctor/app` on all four of its success paths, and that route opens
this file with no existence guard — while parts of its backend have already been
deleted and the rest are flag-gated for deletion.

That asymmetry is the risk this file exists to bound. A page whose buttons call
endpoints that no longer exist doesn't fail loudly; it fails as a spinner that
never resolves, or an error that reads like a network blip. So:

  * every path the page calls must be answerable by the live route table, OR
  * it must be one the page knows is retired and says so plainly.

The second list is deliberately explicit. Adding a path to it is a decision to
show a user "this feature is gone", and that should be a diff someone reads —
not something a regex quietly infers.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
DOCTOR_HTML = REPO / "frontend" / "doctor.html"


# Paths whose backend this cleanup removed, or gates for removal. The page must
# degrade honestly on each: `retirement` in doctor.html turns a 404 into one
# plain notice, with no toast, no Retry button and no console noise.
#
# Grouped the way the page groups them, because that is the unit a user loses.
RETIRED_PREFIXES = (
    # Deleted outright in Phase 3 with routers/eligibility.py.
    "/api/eligibility-checks",
    "/api/eligibility-documents",
    "/api/eligibility-draft-patient",
    "/api/eligibility-draft-patients",
    # Deleted with routers/{postop,preop_retier,triage_explain}.py.
    "/api/episodes/",
    # Deleted with the post-op / pre-op note surface.
    "/api/patient/{}/postop-notes",
    "/api/patient/{}/preop-notes",
    # Deleted with routers/telehealth.py.
    "/api/telehealth/",
)

# Live route table produced by `main.py` with the flag ON. Regenerated here
# rather than read from a checked-in fixture so the test cannot pass against a
# stale snapshot of the app it is meant to be checking.
_ROUTE_DUMP = """
import json, sys
from main import app
rows = [[r.path, sorted(getattr(r, "methods", []) or [])] for r in app.routes]
open(sys.argv[1], "w").write(json.dumps(rows))
"""


def _live_routes() -> set[str]:
    import os

    tmp = tempfile.mkdtemp(prefix="doctor_routes_")
    out = Path(tmp) / "routes.json"
    env = dict(os.environ)
    env["ARCHANGEL_LEGACY_PERIOP"] = "1"
    env["ASCLEPIUS_DB_PATH"] = str(Path(tmp) / "a.db")
    env["ASCLEPIUS_EXPORT_DIR"] = str(Path(tmp) / "exports")
    env["COMMUNITY_DB_PATH"] = str(Path(tmp) / "c.db")
    env["RATE_LIMIT_ENABLED"] = "0"
    proc = subprocess.run([sys.executable, "-c", _ROUTE_DUMP, str(out)],
                          cwd=str(BACKEND), env=env, capture_output=True,
                          text=True, timeout=180)
    assert proc.returncode == 0, f"app failed to boot:\n{proc.stderr[-2000:]}"
    return {p for p, _ in json.loads(out.read_text())}


def _called_paths() -> set[str]:
    """Every API path the page can request, with `${...}` collapsed to `{}`.

    Two shapes need resolving rather than reading literally:

    * ``tenantApiPath(suffix)`` builds ``/api/tenant/${slug}${suffix}``. Read as a
      literal that is ``/api/tenant/{}{}`` — two adjacent interpolations, which
      matches no route template of any length. The suffixes are literal at every
      call site, so they are composed here and the SEVEN REAL PATHS get checked.
      Skipping the builder instead would have quietly excluded the tenant portal,
      which is live and load-bearing, from the only test that covers it.
    * Anything still containing adjacent interpolations after that is a builder
      this function does not understand; it fails the test rather than passing
      silently, so the gap is visible.
    """
    html = DOCTOR_HTML.read_text()
    found = set()
    for m in re.finditer(r"""[`"']((?:/api|/admin)[^`"'\n]*)""", html):
        path = m.group(1).split("?")[0].rstrip("/")
        if not path or path in ("/api", "/admin"):
            continue
        found.add(re.sub(r"\$\{[^}]*\}", "{}", path))

    # Compose tenantApiPath("<literal suffix>") into concrete paths.
    tenant_template = "/api/tenant/{}{}"
    if tenant_template in found:
        found.discard(tenant_template)
        suffixes = re.findall(r"""tenantApiPath\(\s*[`"']([^`"']+)""", html)
        assert suffixes, "tenantApiPath exists but no literal suffixes were found"
        for suffix in suffixes:
            composed = "/api/tenant/{}" + suffix.split("?")[0].rstrip("/")
            found.add(re.sub(r"\$\{[^}]*\}", "{}", composed))
    return found


def _segments(path: str) -> list[str]:
    return re.sub(r"\{[^}]*\}", "{}", path).split("/")


def _served_by(path: str, routes: set[str]) -> str | None:
    """The route template that would answer ``path``, treating every parameter —
    on either side — as exactly one segment."""
    want = _segments(path)
    for tmpl in routes:
        have = _segments(tmpl)
        if len(want) != len(have):
            continue
        if all(a == b or a == "{}" or b == "{}" for a, b in zip(want, have)):
            return tmpl
    return None


def _is_retired(path: str) -> bool:
    return any(_served_by(path, {p}) or path.startswith(p.split("{")[0])
               for p in RETIRED_PREFIXES)


@pytest.fixture(scope="module")
def routes():
    return _live_routes()


def test_the_page_calls_something(routes):
    """Guard the guard: a regex that matched nothing would make every assertion
    below pass while checking nothing at all."""
    called = _called_paths()
    assert len(called) > 30, f"only found {len(called)} API paths in doctor.html"
    assert len(routes) > 300, f"only found {len(routes)} live routes"


def test_every_called_path_is_served_or_visibly_retired(routes):
    orphans = [
        p for p in sorted(_called_paths())
        if not _served_by(p, routes) and not _is_retired(p)
    ]
    assert not orphans, (
        "doctor.html calls paths that are neither served nor declared retired.\n"
        "Either the route came back (drop it from RETIRED_PREFIXES), or the page "
        "gained a call to something that does not exist:\n  "
        + "\n  ".join(orphans)
    )


@pytest.mark.parametrize("prefix", RETIRED_PREFIXES)
def test_a_retired_prefix_really_is_gone(prefix, routes):
    """The other direction. If a route comes back, this list is stale and the page
    would show 'retired' over a working feature — worse than the bug it prevents,
    because it is invisible to the user and to the server."""
    concrete = prefix.replace("{}", "x")
    assert not _served_by(concrete, routes), (
        f"{prefix} is declared retired but IS served — remove it from RETIRED_PREFIXES"
    )


def test_the_page_degrades_honestly_rather_than_silently():
    """The mechanism itself. Asserted on the source because the behaviour only
    appears in a browser against a 404, and a regression here is silent: the page
    would go back to showing a generic error for a permanent condition."""
    html = DOCTOR_HTML.read_text()

    assert "const retirement = (() => {" in html, "the retirement module is gone"
    # A 404 must be routed to the notice, in both call paths.
    assert html.count("throw retirement.error(path)") == 2, (
        "apiJson and streamGeneration must both treat an unrouted 404 as a retirement"
    )
    # No toast for a permanent condition.
    assert "if (typeof msg === \"string\" && msg.includes(retirement.MARKER)) return;" in html, (
        "showToast must stay quiet when the notice is already on screen"
    )
    # No Retry button behind a retired feature.
    assert 'if (retirement.known("/api/eligibility-checks"))' in html, (
        "the eligibility stream must not offer Retry once the feature is retired"
    )


def test_doctor_app_is_on_the_dark_week_watch_list():
    """`/doctor/app` is how anyone reaches this page. Access-log hits there during
    the dark week are the evidence Phase 5 needs to delete it — without them the
    decision is a guess."""
    watch = REPO / "docs" / "dark-week-watch-list.txt"
    assert watch.is_file(), "the dark-week watch list is missing"
    assert "/doctor/app" in watch.read_text(), (
        "/doctor/app must be watched — it is the entry point to the legacy dashboard"
    )
