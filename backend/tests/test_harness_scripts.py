"""Tests for the harness scripts and hooks (Engineering Harness PRD H1–H5).

A sensor nobody tests is a sensor that fails open. Each script here is tested in
BOTH directions — it passes clean input and it actually catches the incident it
was written for — because a checker that never fails is indistinguishable from no
checker at all, and that is how the CSS and dangling-import incidents survived.

Hook timing is asserted, not assumed: H2 caps every hook at 5 seconds.
"""

import json
import pathlib
import subprocess
import sys
import time

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = BACKEND / "scripts"
HOOK_BUDGET_SEC = 5.0


def run(script, *args, stdin=None, cwd=None):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        cwd=str(cwd or BACKEND),
    )


# ─── H2: css_balance.py ──────────────────────────────────────────────────────

def test_css_balance_passes_the_real_stylesheet():
    css = BACKEND.parent / "frontend" / "asclepius" / "asclepius.css"
    assert css.exists()
    assert run("css_balance.py", str(css)).returncode == 0


def test_css_balance_catches_an_unclosed_media_query(tmp_path):
    """The asclepius.css:4814 incident: an @media that never closed, swallowing
    69 rules with no error anywhere."""
    bad = tmp_path / "bad.css"
    bad.write_text(".a { color: red; }\n\n@media (max-width: 900px) {\n  .b { color: blue; }\n")
    proc = run("css_balance.py", str(bad))
    assert proc.returncode == 2
    assert ":3:" in proc.stderr and "never closed" in proc.stderr


def test_css_balance_ignores_braces_in_comments_and_strings(tmp_path):
    """A false alarm on correct CSS gets the hook uninstalled."""
    ok = tmp_path / "tricky.css"
    ok.write_text('/* a { brace } in a comment */\n.c::after { content: "{"; }\n')
    assert run("css_balance.py", str(ok)).returncode == 0


def test_css_balance_reports_an_unmatched_closing_brace(tmp_path):
    bad = tmp_path / "extra.css"
    bad.write_text(".a { color: red; }\n}\n")
    proc = run("css_balance.py", str(bad))
    assert proc.returncode == 2
    assert "no matching" in proc.stderr


# ─── H2: check_dangling_imports.py ───────────────────────────────────────────

def test_no_dangling_imports_in_the_tree():
    proc = run("check_dangling_imports.py")
    assert proc.returncode == 0, proc.stderr


def test_dangling_imports_catches_a_function_local_import_of_a_deleted_module(tmp_path):
    """The incident: `from routers.x import y` inside a function outlived
    routers/x.py. A module-level import fails at boot; this one fails only when
    the function is called, which in a flag-gated path can be never in dev."""
    probe = BACKEND / "_probe_dangling.py"
    probe.write_text("def handler():\n    from routers.deleted_router import thing\n    return thing\n")
    try:
        proc = run("check_dangling_imports.py", "--file", str(probe))
        assert proc.returncode == 2
        assert "routers.deleted_router" in proc.stderr
    finally:
        probe.unlink(missing_ok=True)


def test_dangling_imports_does_not_flag_a_real_module(tmp_path):
    probe = BACKEND / "_probe_ok.py"
    probe.write_text("from asclepius.constants import SUBMISSION_STATUSES\nimport os\n")
    try:
        assert run("check_dangling_imports.py", "--file", str(probe)).returncode == 0
    finally:
        probe.unlink(missing_ok=True)


# ─── H2: route_baseline.py ───────────────────────────────────────────────────

def test_route_snapshot_exists_and_matches_the_live_table():
    """A committed snapshot that no longer matches means a route appeared or
    vanished without anyone deciding it should."""
    snapshot = BACKEND.parent / "docs" / "asclepius" / "ROUTES.json"
    assert snapshot.exists(), "run: python3 scripts/route_baseline.py --snapshot"
    data = json.loads(snapshot.read_text())
    assert data["count"] == len(data["routes"]) > 100
    proc = run("route_baseline.py", "--diff")
    assert proc.returncode == 0, proc.stderr


def test_route_baseline_detects_drift(tmp_path, monkeypatch):
    """Directly exercise the comparison, without booting the app twice."""
    sys.path.insert(0, str(SCRIPTS))
    import route_baseline

    monkeypatch.setattr(route_baseline, "load_snapshot", lambda: ["GET /a", "GET /b"])
    monkeypatch.setattr(route_baseline, "live_routes", lambda: ["GET /a", "GET /c"])
    assert route_baseline.main_cli(["route_baseline.py", "--diff"]) == 1


# ─── H2/H3: merge_readiness.py ───────────────────────────────────────────────

def test_merge_readiness_runs_and_reports():
    proc = run("merge_readiness.py")
    assert proc.returncode in (0, 2)
    assert "merge-base" in proc.stdout or "skipped" in proc.stderr


def test_merge_readiness_stops_on_a_stale_merge_base():
    """The longitudinal-branch incident: a branch cut from a merge-base so old
    that resolving conflicts against it would have dropped a whole feature."""
    proc = run("merge_readiness.py", "--max-behind", "-1")
    if "skipped" in proc.stderr:
        pytest.skip("origin/main not present in this checkout")
    assert proc.returncode == 2
    assert "Rebase before pushing" in proc.stderr


# ─── H2: the hooks themselves, including the 5s budget ───────────────────────

def _hook(script, payload):
    t0 = time.monotonic()
    proc = run(script, stdin=json.dumps(payload))
    return proc, time.monotonic() - t0


def test_post_edit_hook_passes_a_clean_file_within_budget():
    proc, elapsed = _hook("hook_post_edit.py",
                          {"tool_input": {"file_path": str(BACKEND / "ai" / "fake_llm.py")}})
    assert proc.returncode == 0, proc.stderr
    assert elapsed < HOOK_BUDGET_SEC, f"hook took {elapsed:.2f}s (budget {HOOK_BUDGET_SEC}s)"


def test_post_edit_hook_blocks_broken_css_within_budget(tmp_path):
    bad = tmp_path / "bad.css"
    bad.write_text("@media screen {\n  .a { color: red; }\n")
    proc, elapsed = _hook("hook_post_edit.py", {"tool_input": {"file_path": str(bad)}})
    assert proc.returncode == 2
    assert elapsed < HOOK_BUDGET_SEC, f"hook took {elapsed:.2f}s"


def test_post_edit_hook_blocks_a_delete_from_a_protected_table(tmp_path, monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    import hook_post_edit

    store = BACKEND / "asclepius" / "store.py"
    assert hook_post_edit._DELETE_RE.search('cur.execute("DELETE FROM submissions WHERE id=?")')
    assert hook_post_edit._DELETE_RE.search("DELETE FROM earnings")
    # A delete against a non-protected table is allowed.
    assert not hook_post_edit._DELETE_RE.search("DELETE FROM sessions_cache")
    assert store.exists()


def test_post_edit_hook_ignores_a_file_it_does_not_own(tmp_path):
    other = tmp_path / "notes.md"
    other.write_text("# not a checked file type\n")
    proc, _ = _hook("hook_post_edit.py", {"tool_input": {"file_path": str(other)}})
    assert proc.returncode == 0


def test_post_edit_hook_survives_a_missing_path():
    proc, _ = _hook("hook_post_edit.py", {"tool_input": {}})
    assert proc.returncode == 0


def test_pre_push_hook_ignores_non_push_commands():
    for cmd in ("ls -la", "git status", "git commit -m 'x'", "git push --help",
                "echo 'git push' >> notes.txt"):
        proc, elapsed = _hook("hook_pre_push.py", {"tool_input": {"command": cmd}})
        assert proc.returncode == 0, f"{cmd!r} was treated as a push"
        assert elapsed < HOOK_BUDGET_SEC


def test_pre_push_hook_recognises_real_push_commands():
    sys.path.insert(0, str(SCRIPTS))
    import hook_pre_push

    for cmd in ("git push", "git push -u origin main", "git push --force-with-lease",
                "git -C /repo push origin feature"):
        assert hook_pre_push.is_push(cmd), cmd
    for cmd in ("git status", "git push --help", "npm run push", "echo git push"):
        assert not hook_pre_push.is_push(cmd), cmd


def test_pre_push_hook_can_be_bypassed_deliberately(monkeypatch):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "hook_pre_push.py")],
        input=json.dumps({"tool_input": {"command": "git push"}}),
        capture_output=True, text=True, timeout=60, cwd=str(BACKEND),
        env={**__import__("os").environ, "HOOK_SKIP_MERGE_READINESS": "1"},
    )
    assert proc.returncode == 0
    assert "skipped" in proc.stderr


# ─── H1: check_agents_md.py ──────────────────────────────────────────────────

def test_agents_md_is_current():
    proc = run("check_agents_md.py")
    assert proc.returncode == 0, proc.stderr


def test_agents_md_check_catches_a_reintroduced_stale_marker(tmp_path, monkeypatch):
    sys.path.insert(0, str(SCRIPTS))
    import check_agents_md

    original = check_agents_md.AGENTS.read_text()
    try:
        check_agents_md.AGENTS.write_text(original + "\nCareGuide has No database.\n")
        assert check_agents_md.main() == 1
    finally:
        check_agents_md.AGENTS.write_text(original)
    assert check_agents_md.main() == 0


def test_retired_skills_are_not_active():
    skills = BACKEND.parent / ".claude" / "skills"
    for name in ("surgical-risk-triage", "team-eligibility-review", "ehr-extraction"):
        assert not (skills / name).exists(), f"{name} is active again"
        assert (skills / "_retired" / name).exists(), f"{name} is missing from _retired"


# ─── H5: affected_tests.py ───────────────────────────────────────────────────

def test_affected_tests_selects_a_modules_own_test():
    out = run("affected_tests.py", "--files", "ai/fake_llm.py").stdout.split()
    assert "tests/test_fake_llm_provider.py" in out


def test_affected_tests_selects_a_changed_test_file_itself():
    out = run("affected_tests.py", "--files", "tests/test_fake_llm_provider.py").stdout.split()
    assert "tests/test_fake_llm_provider.py" in out


def test_affected_tests_stays_a_subset_not_the_whole_suite():
    """The point is a fast loop. A selector that returns everything is the full
    suite wearing a hat."""
    total = len(list((BACKEND / "tests").glob("test_*.py")))
    out = run("affected_tests.py", "--files", "asclepius/critic.py").stdout.split()
    assert 0 < len(out) < total * 0.5, f"selected {len(out)} of {total}"


def test_affected_tests_is_quiet_when_nothing_relevant_changed():
    out = run("affected_tests.py", "--files", "README.md").stdout.strip()
    assert out == ""
