"""Export exclusivity (audit U5).

The Sep 1 founder meeting put exactly one constraint on otherwise-unrestricted
reuse of incoming clinical data: the licensing agreement. Data may be used for
task creation, for brokering, and may be split and recombined freely, but "unless
they have the licensing agreement, that doesn't mean you can go sell it to 5
other people unless they didn't pay exclusively."

Nothing in the pipeline held that fact, so a second cut overlapping a slice
already sold exclusively shipped silently. These tests pin the behavior that
makes that impossible, and equally pin that a non-exclusive sale, which is every
sale made so far, is completely unaffected.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402
from asclepius import export as asc_export  # noqa: E402
from asclepius import profiles as asc_profiles  # noqa: E402

client = TestClient(A.app)

_FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend" / "asclepius"


@pytest.fixture(autouse=True)
def _isolated():
    A.fresh_store()
    asc_profiles.clear_cache()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


def _admin_h():
    return A.headers_for(A.make_user(_store(), role="admin"))


def _seed_records(store, n: int = 2, specialty: str = "nephrology") -> list:
    """``n`` export-ready preference records, each on its own case.

    Built directly through the store rather than over HTTP: this file is about
    what happens between a set of record ids and a licence, and driving the whole
    labeling pipeline to get there would test the pipeline instead."""
    ids = []
    for _ in range(n):
        tid = "t-" + uuid.uuid4().hex[:12]
        sid = "s-" + uuid.uuid4().hex[:12]
        ids.append(store.insert_record(
            submission_id=sid, task_id=tid, rtype="preference", specialty=specialty,
            status="export_ready",
            payload={
                "type": "preference",
                "prompt": f"Hyperkalemia management {A.uniq(6)}?",
                "chosen": "Calcium gluconate, then dialysis.",
                "rejected": "Observe overnight.",
                "annotator_credential": "board_certified_nephrology",
                "license": "archangel-commercial",
                "ip_cleared": True,
                "contains_phi": False,
                "submission_id": sid,
                "task_id": tid,
            },
        ))
    return ids


def _export(store, *, licensed_to=None, exclusive=False, **kw):
    return asc_export.build_export(
        store, created_by="admin-fixture", include_exported=True,
        licensed_to=licensed_to,
        license_exclusivity=(asc_export.EXCLUSIVE if exclusive
                             else asc_export.NON_EXCLUSIVE),
        **kw,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# The default path is untouched
# ═══════════════════════════════════════════════════════════════════════════════
def test_an_export_that_declares_no_licence_writes_what_it_always_wrote():
    """Exclusivity is opt-in per deal. Every export already in flight declares no
    licence, and for those the manifest, the file set and the register must be
    indistinguishable from before this feature existed, or a safety feature has
    quietly changed what buyers receive."""
    store = _store()
    _seed_records(store, 2)
    manifest = _export(store)

    assert "licensing" not in manifest, "an undeclared export must not grow a licence block"
    on_disk = json.loads((Path(manifest["dir_path"]) / "batch.json").read_text())
    assert "licensing" not in on_disk, "the shipped manifest must not grow a licence block"
    assert store.list_export_licenses() == [], "nothing was licensed, so nothing is recorded"
    assert manifest["record_count"] == 2


def test_a_non_exclusive_licence_records_the_deal_and_blocks_nothing():
    """Non-exclusive is the ordinary sale and the default. Recording it must buy
    the admin a register entry without buying anyone an injunction: the same
    records must still be sellable to the next buyer, and the one after that."""
    store = _store()
    _seed_records(store, 2)

    first = _export(store, licensed_to="lab-one@example.com")
    second = _export(store, licensed_to="lab-two@example.com")
    third = _export(store, licensed_to="lab-three@example.com")

    assert first["licensing"]["exclusivity"] == "non_exclusive"
    assert second["record_count"] == 2 and third["record_count"] == 2
    assert len(store.list_export_licenses(exclusivity="non_exclusive")) == 3
    assert store.list_export_licenses(exclusivity="exclusive") == []


# ═══════════════════════════════════════════════════════════════════════════════
# Exclusivity blocks a second buyer
# ═══════════════════════════════════════════════════════════════════════════════
def test_an_exclusive_sale_blocks_the_same_records_reaching_a_second_buyer():
    """The whole point. Once a buyer has paid for exclusivity on a slice, the next
    export of that slice to somebody else is a contractual breach, and a breach
    that ships is discovered by the counterparty rather than by us."""
    store = _store()
    _seed_records(store, 2)
    _export(store, licensed_to="exclusive-lab@example.com", exclusive=True)

    with pytest.raises(asc_export.ExclusiveLicenseConflict) as exc:
        _export(store, licensed_to="other-lab@example.com")

    assert exc.value.conflicts, "the refusal must carry the conflicting licences"
    assert exc.value.conflicts[0]["buyer_key"] == "exclusive-lab@example.com"


def test_the_refusal_names_the_licence_so_a_human_can_act_on_it():
    """A refusal that says only "export failed" sends the operator hunting for a
    bug. This one is not a bug, it is a contract, and the message has to say which
    contract, held by whom, over how many of these records."""
    store = _store()
    _seed_records(store, 3)
    _export(store, licensed_to="first@example.com", license_label="First Lab",
            exclusive=True)

    with pytest.raises(asc_export.ExclusiveLicenseConflict) as exc:
        _export(store, licensed_to="second@example.com")

    message = str(exc.value)
    conflict = exc.value.conflicts[0]
    assert conflict["license_id"] in message
    assert "First Lab" in message
    assert conflict["export_id"] in message
    assert "3 of these records" in message
    assert conflict["overlap_sample"], "the message must point at concrete records"


def test_an_export_declaring_no_buyer_at_all_is_still_refused():
    """An unlabelled export is not a safe export. We cannot show it is going to the
    holder of the exclusive licence, so treating "no buyer named" as permission
    would leave the widest hole exactly where the fewest checks are."""
    store = _store()
    _seed_records(store, 2)
    _export(store, licensed_to="exclusive-lab@example.com", exclusive=True)

    with pytest.raises(asc_export.ExclusiveLicenseConflict):
        _export(store)


def test_nothing_is_written_and_no_record_is_re_marked_when_a_batch_is_refused():
    """A refusal has to happen before the irreversible half of the export. If the
    bundle directory or the exported marks landed first, a blocked sale would
    still have produced a downloadable zip of the very data it was blocking."""
    store = _store()
    ids = _seed_records(store, 2)
    first = _export(store, licensed_to="exclusive-lab@example.com", exclusive=True)
    before = {r["record_id"]: r["export_id"] for r in store.list_records(status="exported")}
    # The export root is shared by the whole suite, so the assertion is on the
    # delta this test causes, not on the directory's total contents.
    dirs_before = {p.name for p in asc_export.export_root().iterdir() if p.is_dir()}

    with pytest.raises(asc_export.ExclusiveLicenseConflict):
        _export(store, licensed_to="other-lab@example.com")

    after = {r["record_id"]: r["export_id"] for r in store.list_records(status="exported")}
    assert after == before, "a refused batch must not re-stamp any record"
    assert all(after[rid] == first["export_id"] for rid in ids)
    dirs_after = {p.name for p in asc_export.export_root().iterdir() if p.is_dir()}
    assert dirs_after == dirs_before, "a refused batch must not leave a bundle behind"
    assert store.list_exports(limit=5)[0]["export_id"] == first["export_id"]


# ═══════════════════════════════════════════════════════════════════════════════
# The holder keeps their own data
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_exclusive_buyer_can_take_delivery_of_their_own_data_again():
    """Re-cutting a bundle for the buyer who owns the exclusivity breaches nothing,
    and this is not a rare case: re-deliveries after a failed download, a format
    change, or a widened window are routine. A gate that blocked those would be
    turned off within a week."""
    store = _store()
    _seed_records(store, 2)
    _export(store, licensed_to="exclusive-lab@example.com", exclusive=True)

    again = _export(store, licensed_to="Exclusive-Lab@Example.com", exclusive=True)

    assert again["record_count"] == 2
    assert again["licensing"]["exclusivity"] == "exclusive"


def test_one_buyer_spelled_two_ways_is_still_one_buyer():
    """Case and stray whitespace decide, in this design, whether a re-delivery is a
    breach. Matching has to be insensitive to both or the gate fires on the buyer
    it exists to serve."""
    store = _store()
    ids = _seed_records(store, 1)
    _export(store, licensed_to="  Lab@Example.COM ", exclusive=True)

    assert store.exclusive_license_conflicts(ids, buyer_key="lab@example.com") == []
    assert store.exclusive_license_conflicts(ids, buyer_key="lab@other.com")


# ═══════════════════════════════════════════════════════════════════════════════
# Split and recombine: enforcement is per record, not per bundle
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_partly_overlapping_recombination_is_refused_and_names_the_overlap():
    """The meeting's own framing is that data gets split and recombined. So the
    dangerous cut is not a repeat of an earlier bundle, it is a NEW bundle that
    happens to carry one committed record among many free ones. Bundle-level
    bookkeeping misses that entirely; record-level catches it and says how much of
    the batch is affected."""
    store = _store()
    committed = _seed_records(store, 1, specialty="nephrology")
    _seed_records(store, 3, specialty="cardiology")
    _export(store, licensed_to="first@example.com", exclusive=True,
            specialty="nephrology")

    with pytest.raises(asc_export.ExclusiveLicenseConflict) as exc:
        _export(store, licensed_to="second@example.com")

    conflict = exc.value.conflicts[0]
    assert conflict["overlap_count"] == 1, "only the committed record conflicts"
    assert conflict["overlap_sample"] == committed
    # The free records are genuinely free: the same buyer gets them on their own.
    clean = _export(store, licensed_to="second@example.com", specialty="cardiology")
    assert clean["record_count"] == 3


def test_a_wider_recut_cannot_launder_a_committed_record_through_a_new_filter():
    """Re-cutting under a different filter produces a different bundle id and a
    different manifest, so a bundle-keyed check would see a brand-new export and
    wave it through. The commitment is on the records, so the filter is irrelevant."""
    store = _store()
    _seed_records(store, 1, specialty="nephrology")
    _seed_records(store, 2, specialty="cardiology")
    _export(store, licensed_to="first@example.com", exclusive=True,
            specialty="nephrology")

    with pytest.raises(asc_export.ExclusiveLicenseConflict):
        _export(store, licensed_to="second@example.com", grounded_only=False)


# ═══════════════════════════════════════════════════════════════════════════════
# Commitments end
# ═══════════════════════════════════════════════════════════════════════════════
def test_releasing_a_commitment_frees_its_records():
    """Exclusivity is a term of a deal and deals end, get renegotiated, and get
    entered by mistake. Without a release the first typo would take records off the
    market permanently, and the operator's only recourse would be editing SQLite by
    hand on a production box."""
    store = _store()
    _seed_records(store, 2)
    first = _export(store, licensed_to="first@example.com", exclusive=True)
    license_id = first["licensing"]["license_id"]

    with pytest.raises(asc_export.ExclusiveLicenseConflict):
        _export(store, licensed_to="second@example.com")

    store.release_export_license(license_id, released_by="admin-fixture",
                                reason="deal ended")
    freed = _export(store, licensed_to="second@example.com")
    assert freed["record_count"] == 2
    # Released, not deleted: what we promised and when we stopped is the evidence.
    assert store.get_export_license(license_id)["status"] == "released"
    assert store.license_record_ids(license_id), "the membership survives the release"


def test_an_expired_commitment_frees_its_records_without_anyone_doing_anything():
    """A twelve-month exclusive should stop blocking in month thirteen on its own.
    Requiring a human to remember means the block outlives the deal, and the first
    symptom is a sale we refused to make for no reason."""
    store = _store()
    _seed_records(store, 2)
    _export(store, licensed_to="first@example.com", exclusive=True,
            license_expires_at="2020-01-01T00:00:00")

    freed = _export(store, licensed_to="second@example.com")
    assert freed["record_count"] == 2


def test_a_commitment_that_has_not_expired_yet_still_blocks():
    """The mirror of the test above, so an expiry comparison that is backwards
    passes neither."""
    store = _store()
    _seed_records(store, 2)
    _export(store, licensed_to="first@example.com", exclusive=True,
            license_expires_at="2999-01-01T00:00:00")

    with pytest.raises(asc_export.ExclusiveLicenseConflict):
        _export(store, licensed_to="second@example.com")


# ═══════════════════════════════════════════════════════════════════════════════
# Over HTTP
# ═══════════════════════════════════════════════════════════════════════════════
def test_the_export_endpoint_refuses_a_conflicting_cut_with_409_and_the_conflicts():
    """422 would read as "the data is malformed" and 400 as "you typed it wrong".
    Neither is true: the batch is fine and the operator is fine, and what they need
    back is the licence id to go and look at."""
    store = _store()
    _seed_records(store, 2)
    h = _admin_h()
    ok = client.post("/api/asclepius/exports",
                     json={"licensed_to": "first@example.com", "exclusive": True,
                           "include_exported": True}, headers=h)
    assert ok.status_code == 200, ok.text

    clash = client.post("/api/asclepius/exports",
                        json={"licensed_to": "second@example.com",
                              "include_exported": True}, headers=h)
    assert clash.status_code == 409, clash.text
    detail = clash.json()["detail"]
    assert detail["conflicts"][0]["buyer_key"] == "first@example.com"
    assert "exclusive" in detail["message"].lower()


def test_an_exclusive_with_nobody_holding_it_is_refused_at_the_door():
    """An exclusive commitment with no buyer can never be matched against a later
    export, so it would silently protect nobody while looking like protection."""
    store = _store()
    _seed_records(store, 1)
    r = client.post("/api/asclepius/exports",
                    json={"exclusive": True, "include_exported": True},
                    headers=_admin_h())
    assert r.status_code == 400
    assert "licensed_to" in r.json()["detail"]


def test_the_admin_register_shows_what_is_committed_and_can_release_it():
    """Requirement 3: an operator about to sell something must be able to see what
    is already promised, to whom, and end it, without reading the database."""
    store = _store()
    _seed_records(store, 2)
    h = _admin_h()
    built = client.post("/api/asclepius/exports",
                        json={"licensed_to": "first@example.com", "exclusive": True,
                              "license_label": "First Lab", "include_exported": True},
                        headers=h)
    assert built.status_code == 200, built.text
    license_id = built.json()["licensing"]["license_id"]

    view = client.get("/api/asclepius/admin/export/exclusivity", headers=h)
    assert view.status_code == 200, view.text
    payload = view.json()
    assert [lic["license_id"] for lic in payload["active"]] == [license_id]
    assert payload["active"][0]["buyer"] == "First Lab"
    assert payload["committed_record_count"] == 2

    released = client.post(
        f"/api/asclepius/admin/export/exclusivity/{license_id}/release",
        json={"reason": "renegotiated"}, headers=h)
    assert released.status_code == 200, released.text
    assert released.json()["freed_record_count"] == 2

    after = client.get("/api/asclepius/admin/export/exclusivity", headers=h).json()
    assert after["active"] == []
    assert [lic["license_id"] for lic in after["ended"]] == [license_id]
    assert after["ended"][0]["release_reason"] == "renegotiated"

    freed = client.post("/api/asclepius/exports",
                        json={"licensed_to": "second@example.com",
                              "include_exported": True}, headers=h)
    assert freed.status_code == 200, freed.text


def test_the_export_by_case_screen_can_licence_a_cut_and_is_gated_the_same_way():
    """Export-by-case is the surface an operator actually cuts a buyer bundle on,
    so terms recorded anywhere else would be terms nobody records. It goes through
    the same gate: a second exclusive cut of the same cases is refused there too."""
    store = _store()
    _seed_records(store, 2)
    h = _admin_h()
    built = client.post("/api/asclepius/admin/export/case-bundle",
                        json={"licensed_to": "first@example.com", "exclusive": True},
                        headers=h)
    assert built.status_code == 200, built.text
    assert built.json()["licensing"]["exclusivity"] == "exclusive"

    clash = client.post("/api/asclepius/admin/export/case-bundle",
                        json={"licensed_to": "second@example.com"}, headers=h)
    assert clash.status_code == 409, clash.text
    assert clash.json()["detail"]["conflicts"][0]["buyer_key"] == "first@example.com"


def test_releasing_a_licence_twice_is_refused_rather_than_silently_repeated():
    """A second release would overwrite the released_at and the reason on the first
    one, which is the field a dispute reads."""
    store = _store()
    _seed_records(store, 1)
    h = _admin_h()
    built = client.post("/api/asclepius/exports",
                        json={"licensed_to": "first@example.com", "exclusive": True,
                              "include_exported": True}, headers=h)
    license_id = built.json()["licensing"]["license_id"]
    url = f"/api/asclepius/admin/export/exclusivity/{license_id}/release"
    assert client.post(url, json={}, headers=h).status_code == 200
    assert client.post(url, json={}, headers=h).status_code == 409


# ═══════════════════════════════════════════════════════════════════════════════
# The admin surface renders
# ═══════════════════════════════════════════════════════════════════════════════
def _admin_export_js() -> str:
    return (_FRONTEND / "admin_export.js").read_text(encoding="utf-8")


def test_every_class_the_exclusivity_panel_uses_exists_in_the_stylesheet():
    """A class with no rule renders as unstyled text in the middle of the export
    screen, which reads as a broken page rather than a missing style, and no source
    assertion anywhere would notice."""
    css = (_FRONTEND / "asclepius.css").read_text(encoding="utf-8")
    used = set()
    for match in re.finditer(r"class:\s*'([^']+)'", _admin_export_js()):
        for cls in match.group(1).split():
            if cls.startswith("asc-"):
                used.add(cls)
    assert used, "extraction found no classes, so the harness is broken and not the code"
    missing = sorted(c for c in used if f".{c}" not in css)
    assert not missing, f"classes with no rule in asclepius.css: {missing}"


def test_the_export_screen_asks_for_the_licence_and_shows_the_commitments():
    """The register is only worth building if it sits where the decision is made.
    Sending the terms with the cut, and painting what is already committed on the
    same screen, is the whole of requirement 3."""
    js = _admin_export_js()
    assert "licensed_to" in js and "exclusive" in js
    assert "/admin/export/exclusivity" in js
    assert "window.confirm(" in js, "releasing a commitment must not be a stray click"
