"""PRD demo-day P1-3 — the small ones, each of which tells someone something false.

One file because they share a theme rather than a subsystem: in every case the
product had the right answer and reported a different one.

  b  LABEL was defined and never enforced, so the capability table decided
     nothing and a NULL-tier account could draw and submit.
  d  The fourth upload door never recorded provenance, so every byte it accepted
     landed with purpose NULL — which the promotion gate reads as task creation.
  e  ``batch.json`` shipped an internal admin user id and an absolute path on our
     own server to the buyer.
  f  The earnings breakdown counted APPROVED+PAID while task money accrues, so a
     new labeler's first visit said "$75 pending" beside "Tasks labeled: 0".
  g  A note told operators the mint form carries no purpose. It requires one.

(a) and (c) are frontend-only and are asserted in
``test_demo_day_frontend_honesty.py``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EMAIL_DEV_MODE"] = "1"

import tests._asclepius as A  # noqa: E402
from asclepius import capabilities as asc_caps  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402

API = "/api/asclepius"
client = TestClient(A.app)


def _store():
    from asclepius.store import get_store
    return get_store()


# ─── b · LABEL is enforced ────────────────────────────────────────────────────
def test_a_tierless_account_cannot_draw_a_task():
    """'Not yet assigned' denies. The capability module has always said so; the
    two endpoints that matter never asked."""
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator", tier=None)
    r = client.get(f"{API}/tasks/next", headers=A.headers_for(user))
    assert r.status_code == 403, r.text
    assert "tier" in r.json()["detail"].lower()


def test_a_tierless_account_cannot_submit():
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator", tier=None)
    r = client.post(f"{API}/submissions",
                    json={"task_id": "t-nope", "verdict": "A", "confidence": "high"},
                    headers=A.headers_for(user))
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("tier", list(asc_caps.TIERS))
def test_every_real_tier_can_still_label(tier):
    """LABEL belongs to all three tiers. A gate that let only labelers through
    would silently stop reviewers and advisors from doing their own labeling —
    the same 'squeeze three states into two' failure the module exists to end."""
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator", tier=tier)
    r = client.get(f"{API}/tasks/next", headers=A.headers_for(user))
    assert r.status_code != 403, r.text


def test_an_admin_is_not_locked_out_of_the_surface_they_operate():
    store = A.fresh_store()
    admin = A.make_user(store, role="admin", tier=None)
    r = client.get(f"{API}/tasks/next", headers=A.headers_for(admin))
    assert r.status_code != 403, r.text


def test_accounts_that_predate_tiering_are_backfilled_not_locked_out():
    """Enforcement without this migration is a data-supply outage: an account
    from before tiering has verification_status NULL, passes the verification
    gate untouched, and is labeling today."""
    store = A.fresh_store()
    legacy = A.make_user(store, role="evaluator", tier=None)
    assert store.get_user_by_id(legacy["id"])["tier"] is None

    # Re-run the migration the way a redeploy does.
    store._migrate()  # noqa: SLF001

    after = store.get_user_by_id(legacy["id"])
    assert after["tier"] == asc_caps.LABELER
    assert asc_caps.can(after, asc_caps.LABEL)


def test_the_backfill_never_re_grants_a_deliberately_revoked_tier():
    """THE BACKFILL RUNS ON EVERY BOOT, so "grant once" has to be a property of
    the query, not of how often it happens to run. Clearing an account's tier is
    how an admin takes away its ability to label; a migration that hands the
    capability back on the next redeploy is worse than the gap it closed."""
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator", tier=None)
    store._migrate()  # noqa: SLF001
    assert store.get_user_by_id(user["id"])["tier"] == asc_caps.LABELER

    with store._conn() as conn:  # noqa: SLF001
        conn.execute("UPDATE users SET tier = NULL WHERE id = ?", (user["id"],))

    store._migrate()  # noqa: SLF001
    assert store.get_user_by_id(user["id"])["tier"] is None, (
        "tier_assigned_at is already stamped, so this account has had a tier "
        "decided for it and the backfill must not touch it again"
    )


def test_an_approved_account_that_predates_tiering_is_still_covered():
    """Approval cannot happen without a tier today (both doors 400 without one),
    so an approved row with no tier AND no assignment stamp predates tiering.
    Those accounts pass the verification gate and are labeling right now."""
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator", tier=None)
    store.set_verification_status(user["id"], "approved")
    store._migrate()  # noqa: SLF001
    assert store.get_user_by_id(user["id"])["tier"] == asc_caps.LABELER


def test_the_backfill_is_recorded_on_the_row_it_touched():
    """An unexplained tier is indistinguishable from an admin's decision. The
    stamp says which it was."""
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator", tier=None)
    store._migrate()  # noqa: SLF001
    row = store.get_user_by_id(user["id"])
    assert row["tier_assigned_by"] == "migration:tier_backfill"
    assert row["tier_assigned_at"]


def test_the_backfill_never_grants_a_tier_to_a_pending_signup():
    """A pending account is blocked by the verification gate anyway, so granting
    it a tier changes no access — but it would make the admin queue's roster
    chips lie about who has been decided."""
    store = A.fresh_store()
    pending = A.make_user(store, role="evaluator", tier=None)
    store.set_verification_status(pending["id"], "pending")
    store._migrate()  # noqa: SLF001
    assert store.get_user_by_id(pending["id"])["tier"] is None


def test_the_backfill_never_grants_a_tier_to_a_non_contributor_role():
    store = A.fresh_store()
    partner = A.make_user(store, role="data_partner", tier=None)
    store._migrate()  # noqa: SLF001
    assert store.get_user_by_id(partner["id"])["tier"] is None


def test_the_sandbox_demo_account_can_still_draw_a_case():
    """The mock contributor exists to DEMO labeling. Enforcement that silently
    bricked it would be discovered on stage."""
    from asclepius import auth as asc_auth

    store = A.fresh_store()
    mock = asc_auth.ensure_mock_contributor(store)
    if mock is None:
        pytest.skip("mock contributor disabled in this environment")
    assert asc_caps.can(store.get_user_by_id(mock["id"]), asc_caps.LABEL)


# ─── d · the fourth upload door records where it came from ────────────────────
def test_the_provider_account_door_resolves_its_purpose():
    store = A.fresh_store()
    provider = store.provision_data_provider(
        email=f"partner-{A.uniq()}@example.org", password="provider-pass-123456",
        org_name="Brokered Data Co",
    )
    # Set from the ADMIN side. The provider router is forbidden from naming this
    # at all (PRD-I §3.3, enforced by tests/test_purpose_isolation.py) — the door
    # names its own account row and the store joins the value forward.
    store.set_data_provider_purpose(provider["provider_id"],
                                    asc_ingestion.PURPOSE_BROKERING)
    upload_id = store.new_upload_id()
    store.insert_ingest_upload(
        upload_id=upload_id, link_id="account", partner_id=provider["provider_id"],
        filename="b.zip", sha256="deadbeef", size_bytes=10, raw_path="/tmp/x",
        source_ip=None)
    assert store.get_ingest_upload(upload_id)["purpose"] is None

    store.attach_upload_provenance(upload_id, provider_id=provider["provider_id"])

    purpose = store.get_ingest_upload(upload_id)["purpose"]
    assert purpose == asc_ingestion.PURPOSE_BROKERING
    assert asc_ingestion.is_brokering(purpose), (
        "brokering data that lands NULL is read as task creation by the gate — "
        "one admin click from a physician's queue"
    )


def test_an_unset_provider_purpose_stays_unset_rather_than_being_invented():
    """NULL is 'nobody has decided', an admin work item. A door that guessed
    would reproduce the failure the whole gate exists to prevent."""
    store = A.fresh_store()
    provider = store.provision_data_provider(
        email=f"partner-{A.uniq()}@example.org", password="provider-pass-123456")
    upload_id = store.new_upload_id()
    store.insert_ingest_upload(
        upload_id=upload_id, link_id="account", partner_id=provider["provider_id"],
        filename="b.zip", sha256="deadbeef", size_bytes=10, raw_path="/tmp/x",
        source_ip=None)
    store.attach_upload_provenance(upload_id, provider_id=provider["provider_id"])
    assert store.get_ingest_upload(upload_id)["purpose"] is None


def test_the_admin_endpoint_refuses_a_purpose_that_is_not_one():
    store = A.fresh_store()
    admin_h = A.headers_for(A.make_user(store, role="admin"))
    provider = store.provision_data_provider(
        email=f"p-{A.uniq()}@example.org", password="provider-pass-123456")
    r = client.post(f"{API}/admin/data-providers/{provider['provider_id']}/purpose",
                    json={"purpose": "resale"}, headers=admin_h)
    assert r.status_code == 400
    assert "purpose must be one of" in r.json()["detail"]


def test_the_admin_endpoint_sets_it_and_the_next_upload_carries_it():
    store = A.fresh_store()
    admin_h = A.headers_for(A.make_user(store, role="admin"))
    provider = store.provision_data_provider(
        email=f"p-{A.uniq()}@example.org", password="provider-pass-123456")
    r = client.post(f"{API}/admin/data-providers/{provider['provider_id']}/purpose",
                    json={"purpose": asc_ingestion.PURPOSE_BROKERING}, headers=admin_h)
    assert r.status_code == 200, r.text

    upload_id = store.new_upload_id()
    store.insert_ingest_upload(
        upload_id=upload_id, link_id="account", partner_id=provider["provider_id"],
        filename="b.zip", sha256="deadbeef", size_bytes=10, raw_path="/tmp/x",
        source_ip=None)
    store.attach_upload_provenance(upload_id, provider_id=provider["provider_id"])
    assert store.get_ingest_upload(upload_id)["purpose"] == asc_ingestion.PURPOSE_BROKERING


def test_the_admin_endpoint_404s_for_an_unknown_provider():
    store = A.fresh_store()
    admin_h = A.headers_for(A.make_user(store, role="admin"))
    r = client.post(f"{API}/admin/data-providers/nope/purpose",
                    json={"purpose": asc_ingestion.PURPOSE_TASK_CREATION},
                    headers=admin_h)
    assert r.status_code == 404, "a silent success on a typo'd id is worse than an error"


def test_the_provider_router_never_names_the_distinction_after_this_change():
    """Belt and braces beside tests/test_purpose_isolation.py: the control added
    for (d) must live on the admin surface, never on the door."""
    src = (Path(__file__).resolve().parents[1] / "routers" / "asclepius_provider.py")
    raw = src.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    assert "purpose" not in code.lower()
    assert "attach_upload_provenance" in code, "the door still records WHERE it came from"


# ─── e · batch.json describes the data, not us ────────────────────────────────
def test_the_shipped_manifest_carries_no_internal_identity(tmp_path):
    from asclepius.export import _shippable_manifest

    full = {"export_id": "e-1", "record_count": 3, "created_by": "u-admin-7",
            "dir_path": "/srv/asclepius/exports/e-1", "destination": "local_disk",
            "profile": "default"}
    shipped = _shippable_manifest(full)
    assert "created_by" not in shipped, "an internal admin user id shipped to a buyer"
    assert "dir_path" not in shipped, "an absolute path on our own server"
    assert "destination" not in shipped
    # Everything that describes the DATA survives.
    assert shipped["export_id"] == "e-1"
    assert shipped["record_count"] == 3
    assert shipped["profile"] == "default"


def test_the_audit_trail_keeps_what_the_bundle_drops(tmp_path):
    """Stripping the buyer's copy must not cost us the record of who built it."""
    from asclepius.export import _shippable_manifest

    full = {"export_id": "e-1", "created_by": "u-admin-7"}
    assert _shippable_manifest(full)["export_id"] == "e-1"
    assert full["created_by"] == "u-admin-7", "the source dict is not mutated"


# ─── f · the count and the money come from the same states ────────────────────
def test_pending_money_is_reported_with_a_pending_count():
    """The failure was arithmetic the reader had to do and could not: the money
    said 'pending', the count said zero, and both were labelled 'Tasks labeled'."""
    from asclepius.payments import _line, ACCRUED, APPROVED, PAID, KIND_TASK

    totals = {
        ACCRUED: {KIND_TASK: {"n": 1, "cents": 7500}},
        APPROVED: {KIND_TASK: {"n": 2, "cents": 15000}},
        PAID: {KIND_TASK: {"n": 1, "cents": 7500}},
    }
    line = _line(totals, KIND_TASK, "Tasks labeled", 7500)
    assert line["count"] == 3 and line["cents"] == 22500
    assert line["pending_count"] == 1 and line["pending_cents"] == 7500


def test_the_first_submitted_task_is_never_reported_as_zero_labeled():
    from asclepius.payments import _line, ACCRUED, KIND_TASK

    line = _line({ACCRUED: {KIND_TASK: {"n": 1, "cents": 7500}}},
                 KIND_TASK, "Tasks labeled", 7500)
    assert line["pending_count"] == 1, (
        "a labeler with one submitted task saw '$75 pending' beside "
        "'Tasks labeled: 0' — the page contradicting itself"
    )


def test_void_money_is_in_neither_half():
    """A rejected task is reported on its own row with the reason attached. Folding
    it into either total would make a number move with no explanation beside it."""
    from asclepius.payments import _line, VOID, KIND_TASK

    line = _line({VOID: {KIND_TASK: {"n": 1, "cents": 7500}}},
                 KIND_TASK, "Tasks labeled", 7500)
    assert line["count"] == 0 and line["cents"] == 0
    assert line["pending_count"] == 0 and line["pending_cents"] == 0


def test_the_live_endpoint_serves_the_pending_half():
    store = A.fresh_store()
    user = A.make_user(store, role="evaluator")
    body = client.get(f"{API}/earnings", headers=A.headers_for(user)).json()
    for line in body["lines"]:
        assert "pending_count" in line and "pending_cents" in line


# ─── g · the note describes the code as it is ─────────────────────────────────
def test_the_mint_form_actually_requires_a_purpose():
    store = A.fresh_store()
    admin_h = A.headers_for(A.make_user(store, role="admin"))
    r = client.post(f"{API}/admin/upload-links",
                    json={"partner_id": "p1", "expires_hours": 24},
                    headers=admin_h)
    # 422 (the field is required on the model) or 400 (the value is checked in
    # the handler) — either way the form refuses, which is the premise the note
    # under test has to describe.
    assert r.status_code in (400, 422), r.text


def test_the_note_does_not_claim_the_mint_form_omits_purpose():
    from routers.asclepius_admin import _link_purpose_note

    note = _link_purpose_note() or ""
    assert note, "the note is what tells an operator whether an unset purpose is a bug"
    assert "carry no purpose" not in note, (
        "the mint form 400s without a purpose; this note sent operators looking "
        "for a field that is mandatory, and taught them 'Purpose not set' is normal"
    )
    assert "mandatory" in note or "always carry one" in note


# ─── P1-1 · a public signup must not mint an operator ─────────────────────────
#
# The most serious finding in the audit and the only one with no visible symptom:
# /api/onboarding/self-serve is PUBLIC (it exists so a physician can click
# "Become a contributor" on the landing page), and the wizard it starts ended by
# provisioning role="admin". A YC partner creating an account to try the product
# got the admin console.
def test_the_self_serve_wizard_never_provisions_an_admin():
    """Asserted on the CODE, comments stripped — the comment above the call site
    explains what it used to be, and a naive grep would read the explanation as
    the violation and push the next maintainer to delete the reasoning."""
    src = Path(__file__).resolve().parents[1] / "routers" / "onboarding.py"
    raw = src.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    finish = code[code.index('"/asclepius/finish"'):]
    nxt = finish.find("@router.post", 40)
    if nxt != -1:
        finish = finish[:nxt]
    assert 'role="admin"' not in finish, (
        "/asclepius/finish is reachable from the public /self-serve door; an "
        "admin here is exempt from the verification gate and unscoped across "
        "every require_admin endpoint"
    )
    assert 'role="evaluator"' in finish


def test_the_admin_gate_is_still_a_gate():
    """The fix is the role, not the check — so the check itself must still deny.
    If require_admin ever stops denying, the role change stops mattering."""
    store = A.fresh_store()
    contributor = A.make_user(store, role="evaluator")
    r = client.get(f"{API}/verify/queue", headers=A.headers_for(contributor))
    assert r.status_code == 403, r.text


def test_a_contributor_cannot_read_another_physicians_dossier():
    """The concrete consequence that mattered: the verification dossier carries
    NPI, credentials and the parsed CV of every physician on the platform."""
    store = A.fresh_store()
    contributor = A.make_user(store, role="evaluator")
    other = A.make_user(store, role="evaluator")
    r = client.get(f"{API}/verify/queue/{other['id']}",
                   headers=A.headers_for(contributor))
    assert r.status_code == 403, r.text


def test_a_contributor_cannot_approve_themselves():
    store = A.fresh_store()
    contributor = A.make_user(store, role="evaluator")
    store.set_verification_status(contributor["id"], "pending")
    r = client.post(f"{API}/verify/queue/{contributor['id']}/approve",
                    json={"tier": "labeler"}, headers=A.headers_for(contributor))
    assert r.status_code == 403, r.text


def test_the_downloaded_zip_never_carries_internal_identity_either():
    """The leak had TWO copies and the first fix only covered one.

    ``build_export`` writes batch.json to the export directory, and ``zip_export``
    normally zips that (sanitized) file. But when the directory is gone — purged,
    or lost to a redeploy on ephemeral storage — it falls back to rebuilding
    batch.json from the STORED manifest, which is the internal one. That fallback
    is the copy a buyer downloads, so sanitizing only the on-disk file left the
    leak on the path that actually delivers.
    """
    import zipfile
    from asclepius.export import MANIFEST_NAME, zip_export

    blob = zip_export({
        "dir_path": "/nonexistent/export/dir",   # force the fallback branch
        "manifest": {"export_id": "e-1", "record_count": 2,
                     "created_by": "u-admin-7",
                     "dir_path": "/srv/asclepius/exports/e-1",
                     "destination": "local_disk"},
    })
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        shipped = json.loads(zf.read(MANIFEST_NAME))
    assert "created_by" not in shipped, "an internal admin id in the buyer's zip"
    assert "dir_path" not in shipped, "an absolute server path in the buyer's zip"
    assert shipped["export_id"] == "e-1" and shipped["record_count"] == 2


def test_the_manifest_strip_list_holds():
    """The advisor view that duplicated this rule is retired; the shipped
    bundle's strip list is the one copy left, and it still strips the
    operator-only keys."""
    from asclepius.export import _shippable_manifest

    full = {"keep": 1, "created_by": "u", "dir_path": "/p", "destination": "d"}
    assert _shippable_manifest(full) == {"keep": 1}
