"""Advisor PRD Phases 4–5 — provenance disclosure and the surfaces.

The commercial argument of this company is that it names things honestly — that
it will not print "Cohen's κ" over a number that is not κ. A buyer sophisticated
enough to care about that is sophisticated enough to ask who signed off and what
their interest is. So:

  * every advisor-authored record carries ``related_party: true``;
  * the data dictionary explains the field in the same breath as the credential;
  * the quality report names the advisory count in the annotator pool;
  * and advisors are NOT excluded from acceptance or κ — excluding them would be
    a different kind of dishonesty.

Plus the frontend guards: three tiers in every vocabulary map, and a VISIBLE
error when the advisor module fails to load.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import packaging  # noqa: E402
from asclepius import store as asc_store  # noqa: E402

client = TestClient(A.app)

FRONTEND = pathlib.Path(__file__).resolve().parent.parent.parent / "frontend" / "asclepius"


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    yield


def _advisor():
    store = asc_store.get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology",
                    board_cert="ABIM — Nephrology")
    return store.appoint_advisor(u["id"], agreement_ref="AGR-1", appointed_by="admin@x")


def _labeler():
    store = asc_store.get_store()
    u = A.make_user(store, role="evaluator", specialty="nephrology",
                    board_cert="ABIM — Nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET tier = 'labeler' WHERE id = ?", (u["id"],))
    return store.get_user_by_id(u["id"])


def _submission(store, user, task):
    return store.insert_submission(
        submission_id=f"sub-{uuid.uuid4().hex[:10]}", task_id=task["task_id"],
        evaluator_id=user["id"], verdict="A_better", chosen_id="A", rejected_id="B",
        confidence="high", time_spent_sec=90,
        payload={"verdict": "A_better", "chosen_id": "A", "rejected_id": "B",
                 "rationale": "A is correct for this patient.",
                 "prompt_review": {"reviewed": True, "verdict": "valid"}},
        annotator=store.annotator_block(user), dedupe_hash=None)


def _task(store):
    return store.insert_task(
        prompt="A 62-year-old with rising creatinine…", specialty="nephrology",
        candidate_answers=[{"id": "A", "text": "Lower dialysate K+, give calcium."},
                           {"id": "B", "text": "Start insulin-dextrose immediately."}])


# ═══ §5.1 — the flag ═════════════════════════════════════════════════════════
def test_an_advisor_authored_record_ships_related_party_true():
    store = asc_store.get_store()
    task = _task(store)
    advisor = _advisor()
    records = packaging.package_submission(task, _submission(store, advisor, task),
                                           store=store)
    assert records, "the submission produced no records"
    for rec in records:
        assert rec["related_party"] is True
        # The credential is real AND the relationship is disclosed, in the same
        # block. That pairing is the whole point — the flag is not a demotion.
        assert rec["annotator_credential"] == "ABIM — Nephrology"


def test_an_ordinary_physicians_record_ships_related_party_false():
    store = asc_store.get_store()
    task = _task(store)
    labeler = _labeler()
    records = packaging.package_submission(task, _submission(store, labeler, task),
                                           store=store)
    assert records
    for rec in records:
        assert rec["related_party"] is False


def test_the_flag_resolves_from_the_same_source_as_the_credential():
    """Two separately-resolved facts about one person is how one of them ends up
    stale on a record that ships. The annotator block carries both."""
    store = asc_store.get_store()
    advisor = _advisor()
    block = store.annotator_block(advisor)
    assert block["tier"] == "advisor"
    assert block["credential"] == "ABIM — Nephrology"

    # Hydrated block, no store: the flag still resolves.
    assert packaging._annotator_related_party({"annotator": block}) is True
    # Unhydrated block, store available: it hydrates from the users table.
    assert packaging._annotator_related_party(
        {"evaluator_id": advisor["id"], "annotator": {}}, store) is True
    assert packaging._annotator_related_party(
        {"evaluator_id": _labeler()["id"], "annotator": {}}, store) is False


def test_every_shipped_record_carries_the_key_even_when_false():
    """A key that is present-when-true and absent-when-false is a key a buyer
    cannot filter on, and cannot distinguish from 'this export predates the
    field'."""
    store = asc_store.get_store()
    task = _task(store)
    for user in (_advisor(), _labeler()):
        for rec in packaging.package_submission(task, _submission(store, user, task),
                                                store=store):
            assert "related_party" in rec
            assert isinstance(rec["related_party"], bool)


def test_the_flag_is_as_of_authorship_not_as_of_export():
    """A contributor appointed AFTER writing a record held no financial interest
    when they wrote it, so that record ships ``false``. Back-stamping their
    history would be the same inaccuracy pointing the other way — and the
    records they author from then on carry ``true``."""
    store = asc_store.get_store()
    task = _task(store)
    person = _labeler()

    before = _submission(store, person, task)          # written as a labeler
    store.appoint_advisor(person["id"], agreement_ref="AGR-LATER",
                          appointed_by="admin@x")
    after = _submission(store, store.get_user_by_id(person["id"]), task)

    assert all(r["related_party"] is False
               for r in packaging.package_submission(task, before, store=store))
    assert all(r["related_party"] is True
               for r in packaging.package_submission(task, after, store=store))


# ═══ §5.1 — the disclosure text ══════════════════════════════════════════════
def test_the_data_dictionary_documents_related_party():
    from asclepius.export import _data_dictionary_md

    md = _data_dictionary_md("default")
    assert "`related_party`" in md
    # It must say BOTH things: the credential is real, and the relationship
    # exists. Either sentence alone is the misleading half.
    row = next(line for line in md.splitlines() if "`related_party`" in line)
    assert "advisory relationship" in row
    assert "equity" in row
    assert "credentials are unchanged" in row


def test_the_quality_report_names_the_advisory_count_in_the_pool():
    from asclepius.export import _quality_report_md

    records = [
        {"type": "preference", "payload": {"annotator_id_hashed": "aaa", "related_party": True}},
        {"type": "preference", "payload": {"annotator_id_hashed": "bbb", "related_party": False}},
        {"type": "preference", "payload": {"annotator_id_hashed": "ccc", "related_party": False}},
    ]
    md = _quality_report_md(export_id="exp-1", profile_name="default",
                            records=records, stats={})
    assert "Annotator pool" in md
    assert "3 physicians" in md
    assert "1 advisory (related party)" in md


def test_a_pool_with_no_advisors_says_nothing_about_advisors():
    from asclepius.export import _quality_report_md

    records = [{"type": "preference", "payload": {"annotator_id_hashed": "bbb", "related_party": False}}]
    md = _quality_report_md(export_id="exp-1", profile_name="default",
                            records=records, stats={})
    assert "1 physician" in md
    assert "related party" not in md.lower().split("## annotator pool")[1][:200]


def test_no_per_advisor_quality_score_is_computed_anywhere():
    """§5.2. With n=1 advisor a per-person acceptance rate identifies him, and
    scoring an unpaid advisor individually is a good way to stop having one."""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "asclepius" / "export.py").read_text(encoding="utf-8")
    for banned in ("advisor_acceptance", "acceptance_by_advisor",
                   "advisor_accept_rate", "by_advisor"):
        assert banned not in src


def test_advisors_are_not_excluded_from_the_statistics():
    """Their labels are legitimate physician judgment and belong in expert
    acceptance and in κ. Excluding them would be its own dishonesty."""
    for module in ("agreement.py", "export.py"):
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "asclepius" / module).read_text(encoding="utf-8")
        # No filter anywhere that drops advisory work from a quality statistic.
        assert not re.search(r"related_party\s*(!=|is not)\s*True", src)
        assert "exclude_advisor" not in src


# ═══ §6 — the surfaces ═══════════════════════════════════════════════════════
def test_the_admin_roster_counts_and_labels_three_tiers():
    store = asc_store.get_store()
    admin = A.make_user(store, role="admin")
    advisor = _advisor()
    _labeler()
    body = client.get("/api/asclepius/admin/physicians",
                      headers=A.headers_for(admin)).json()
    assert body["counts"]["advisors"] == 1
    assert body["counts"]["labelers"] == 1
    # An advisor must NOT be swept into "unassigned" — that is the two-state
    # check wearing a different hat.
    assert body["counts"]["unassigned"] == 0
    row = next(p for p in body["physicians"] if p["id"] == advisor["id"])
    assert row["tier"] == "advisor"
    assert row["tier_word"] == "Medical Advisor"
    assert row["slack_role"] == "Medical Advisor"
    assert row["compensation_model"] == "equity_only"


def test_the_frontend_tier_vocabulary_knows_all_three_tiers():
    """A missing case in the frontend map does not throw — it renders
    'Unassigned' over a real advisor. Same class of bug, one layer out."""
    src = (FRONTEND / "admin_physicians.js").read_text(encoding="utf-8")
    for tier in ("labeler", "reviewer", "advisor"):
        assert f"'{tier}'" in src, f"the roster never mentions {tier!r}"
    assert "Medical Advisor" in src
    # The filter chip exists, and "unassigned" is a membership test rather than
    # a chain of not-equals that a fourth tier would fall through.
    assert "advisors" in src
    assert "TIERS.includes" in src

    # The verification queue can appoint straight to advisor, and asks for the
    # agreement when it does.
    onboarding = (FRONTEND / "onboarding.js").read_text(encoding="utf-8")
    assert "value: 'advisor'" in onboarding
    assert "agreement" in onboarding.lower()


def test_the_advisor_module_is_its_own_file_and_is_actually_included():
    """The last audit's lesson: a section that is written but never included in
    index.html is a feature that shipped invisible for an entire build round."""
    advisor_js = FRONTEND / "advisor.js"
    assert advisor_js.exists()
    src = advisor_js.read_text(encoding="utf-8")
    assert "window.AdvisorSection" in src

    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "advisor.js" in index, "advisor.js is never loaded by the portal"
    # And it must load BEFORE asclepius.js, which mounts it.
    assert index.index("advisor.js") < index.index("asclepius.js\"")


def test_a_missing_advisor_module_renders_a_visible_error_not_a_placeholder():
    src = (FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    mount = src.split("function renderAdvisorView")[1][:1400]
    assert "window.AdvisorSection" in mount
    assert "asc-error" in mount, (
        "a missing advisor module must fail VISIBLY — a quiet fallback is how a "
        "shipped feature stayed invisible for a whole build round")


def test_the_advisor_rail_item_is_gated_on_capability_not_on_a_tier_string():
    src = (FRONTEND / "asclepius.js").read_text(encoding="utf-8")
    assert "capability: 'refer'" in src
    assert "sessionCan" in src
    # The client must never re-derive "is this an advisor?" from a tier string —
    # that is the two-state check pushed into the frontend.
    rail = src.split("const RAIL_ITEMS")[1][:900]
    assert "'advisor'" not in rail.replace("dest: 'advisor'", "")


def test_the_advisor_section_never_renders_credentialing_internals():
    """The referrals table can only show what the server sends, and the server
    sends a whitelist. This asserts the client did not add a column that would
    demand more."""
    src = (FRONTEND / "advisor.js").read_text(encoding="utf-8")
    for banned in ("r.npi", "r.tier_score", "r.verification_status",
                   "r.verification_notes", "r.board_cert"):
        assert banned not in src, f"the advisor section reaches for {banned}"
