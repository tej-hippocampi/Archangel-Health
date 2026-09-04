"""Case Generation Fix PRD — why real-record generation yielded nothing, and the
admin cleanup around it.

Part A is executed against the committed patient-3 chart, in memory, through the
same adapter → unify → merge → normalize steps ``ingestion.process_upload`` runs,
so every number here is a number the pipeline produces on real data rather than
on a fixture written to pass. Part B pins the read-path additions and the
removed UI.

What running it found, against the PRD's diagnosis (recorded so the next reader
does not re-derive it):

* §A1 was real: 0 of the 79 text notes carried an offset. Fixed in the adapter.
* §A2's split into "3 case(s)" does NOT reproduce on this tree — the fixture
  door already declares ``manifest.patient_key`` and ``unify_patient_keys``
  already folds unkeyed fragments into the one keyed patient. Pinned here so it
  cannot regress, and the report now names the absorption.
* §A3/§A4 were real (anchor pool was labs-only; structured dates ignored the
  record's day-first reading). Fixed.
* §A5: patient-3's own signal sits just under the floor on every encounter
  (hepatology and nephrology within a few points). The declaration is the fix,
  and the row now says so before Build.
"""
from __future__ import annotations

import io
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _asclepius as A  # noqa: E402

from asclepius import case_formats as cf  # noqa: E402
from asclepius import ingestion as ING  # noqa: E402
from asclepius import patient_fixtures as PF  # noqa: E402
from asclepius import real_cases as RC  # noqa: E402
from asclepius import timeline as TL  # noqa: E402
from asclepius.adapters import note_text  # noqa: E402

_FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend" / "asclepius"


# ═══════════════════════════════════════════════════════════════════════════════
# §A1 — the note adapter keeps the note's date
# ═══════════════════════════════════════════════════════════════════════════════
_HEADERED = (
    "Document index: 72\nDocument type: discharge summary\nService date: 2025-07-01\n"
    "Age on report: 76Y    Sex: F\n\nCONTENT\n-----\nDischarge Summary\n"
    "Admission Date: 28/06/2025\nDiagnosis: K/C HCV / DCLD\n")


def test_note_date_comes_from_the_service_date_header():
    frag = note_text.parse(_HEADERED, manifest={"filename": "072_2025-07-01_discharge-summary.txt"})
    assert frag["notes"][0]["collected_at"] == "2025-07-01"
    assert frag["notes"][0]["note_type"] == "Discharge"


def test_manifest_date_outranks_the_header_and_the_filename():
    frag = note_text.parse(_HEADERED, manifest={"filename": "072_2025-07-01_x.txt", "date": "2025-07-03"})
    assert frag["notes"][0]["collected_at"] == "2025-07-03"


def test_filename_date_is_the_fallback_when_no_header_carries_one():
    frag = note_text.parse("Pt improving, ascites down.",
                           manifest={"filename": "notes/041_2024-01-09_cbc-hematology.txt"})
    assert frag["notes"][0]["collected_at"] == "2024-01-09"
    assert frag["notes"][0]["note_type"] == "Lab report"


def test_admission_date_inside_the_body_does_not_date_the_document():
    """``Admission Date`` dates the stay, not the summary written at discharge.
    Reading it would place the summary BEFORE the course it narrates — and
    before the decision point it must stay sealed behind. Undated fails closed."""
    text = "Discharge Summary\nAdmission Date: 28/06/2025\nDischarge Date: 01/07/2025\n"
    frag = note_text.parse(text, manifest={"filename": "summary.txt"})
    assert "collected_at" not in frag["notes"][0]


@pytest.mark.parametrize("bad", ["2025-13-45", "13/13/2025", "31/04/2025", "12345678", "n/a"])
def test_a_malformed_header_date_leaves_the_note_undated_rather_than_quarantining(bad):
    """Before §A1 the header was ignored; a chart with a mangled header ingested
    fine. Emitting a date-shaped but unparseable value would quarantine it."""
    frag = note_text.parse(f"Service date: {bad}\n\nfindings", manifest={"filename": "x.txt"})
    assert "collected_at" not in frag["notes"][0]
    case, report = TL.normalize_timeline({"notes": frag["notes"], "lab_panels": [
        {"panel": "CBC", "collected_at": "2026-04-23", "results": []}]})
    assert report["unresolved"] == []


def test_unknown_date_stays_undated_rather_than_quarantining_the_chart():
    """``Service date: unknown-date`` is a statement that the page has no date. It
    must NOT be emitted as one — an unparseable ``collected_at`` reaches
    ``_assign_offset`` as an unresolved token and quarantines the whole chart."""
    text = "Document type: radiology report\nService date: unknown-date\n\nModality: Echo\n"
    frag = note_text.parse(text, manifest={"filename": "058_unknown-date_radiology-report.txt"})
    assert "collected_at" not in frag["notes"][0]
    assert frag["notes"][0]["note_type"] == "Radiology"
    # and through the timeline it is simply an undated note, not a hold
    case, report = TL.normalize_timeline({"notes": frag["notes"], "lab_panels": [
        {"panel": "CBC", "collected_at": "2026-04-23", "results": []}]})
    assert report["unresolved"] == []
    assert "collected_offset_days" not in case["notes"][0]


@pytest.mark.parametrize("name,expected", [
    ("072_2025-07-01_discharge-summary.txt", "Discharge"),
    ("075_2026-04-23_clinical-note.txt", "Progress"),
    ("057_2026-04-23_radiology-report.txt", "Radiology"),
    ("009_2026-04-23_rft-renal.txt", "Lab report"),
    ("003_2026-04-23_lft.txt", "Lab report"),
    ("011_2026-04-23_electrolytes.txt", "Lab report"),
    ("001_2026-04-24_tumor-marker.txt", "Lab report"),
    ("consult_gastro.txt", "Consult"),
    ("anything.txt", "Progress"),
])
def test_note_type_from_the_filename_token(name, expected):
    assert note_text.parse("x" * 50, manifest={"filename": name})["notes"][0]["note_type"] == expected


def test_manifest_note_type_still_outranks_the_filename():
    frag = note_text.parse("x" * 50, manifest={"filename": "009_2026-04-23_rft-renal.txt",
                                               "note_type": "Consult"})
    assert frag["notes"][0]["note_type"] == "Consult"


# ═══════════════════════════════════════════════════════════════════════════════
# §A4 — structured dates honour the record's day-first reading
# ═══════════════════════════════════════════════════════════════════════════════
def test_parse_datetime_reads_a_slash_date_in_the_records_order():
    assert TL.parse_datetime("06/01/2024") == date(2024, 6, 1)                      # default unchanged
    assert TL.parse_datetime("06/01/2024", date_order=TL.DATE_ORDER_MDY) == date(2024, 6, 1)
    assert TL.parse_datetime("06/01/2024", date_order=TL.DATE_ORDER_DMY) == date(2024, 1, 6)
    # An unambiguous token is read the only way it can be, whatever the order.
    assert TL.parse_datetime("25/01/2024", date_order=TL.DATE_ORDER_MDY) == date(2024, 1, 25)


def test_a_dmy_csv_panel_lands_on_the_right_day():
    """A day-first record whose CSV writes ``06/01/2024``: the panel is January 6,
    seven days before the January 13 index — not June 1, 140 days after it."""
    frags = {
        "lab_panels": [
            {"panel": "RFT", "collected_at": "06/01/2024", "results": []},
            {"panel": "RFT", "collected_at": "13/01/2024", "results": []},
        ],
        "notes": [{"note_type": "Progress", "author_role": "x",
                   "text": "Seen 25/12/2023 and again 13/01/2024 with rising creatinine."}],
    }
    case, report = TL.normalize_timeline(frags)
    assert report["date_order"] == TL.DATE_ORDER_DMY
    assert [p["collected_offset_days"] for p in case["lab_panels"]] == [-7, 0]
    assert report["unresolved"] == []


def test_structured_slash_dates_count_as_evidence_for_the_order():
    """No free text at all; the CSV's own ``25/01/2024`` says day-first."""
    frags = {"lab_panels": [
        {"panel": "A", "collected_at": "25/01/2024", "results": []},
        {"panel": "B", "collected_at": "06/01/2024", "results": []}]}
    case, report = TL.normalize_timeline(frags)
    assert report["date_order"] == TL.DATE_ORDER_DMY
    assert sorted(p["collected_offset_days"] for p in case["lab_panels"]) == [-19, 0]


# ═══════════════════════════════════════════════════════════════════════════════
# §A3 — a notes-only fragment anchors on its latest note; the hold names itself
# ═══════════════════════════════════════════════════════════════════════════════
def test_a_notes_only_fragment_anchors_on_its_latest_note():
    frags = {"notes": [
        {"note_type": "Progress", "author_role": "x", "collected_at": "2025-07-01",
         "text": "Admitted 2025-06-28 with abdominal pain."},
        {"note_type": "Discharge", "author_role": "x", "collected_at": "2025-07-07",
         "text": "Discharged 2025-07-07 improved."},
    ]}
    case, report = TL.normalize_timeline(frags)
    assert report["unresolved"] == []
    assert report["index_source"] == "latest_notes"
    assert report["index_pool"] == {"notes": 2}
    assert [n["collected_offset_days"] for n in case["notes"]] == [-6, 0]
    assert "[day -9]" in case["notes"][0]["text"]
    assert "collected_at" not in case["notes"][0]


def test_a_note_dated_after_the_last_lab_moves_the_anchor_and_says_so():
    """Widening the pool means the index is the latest DATED ITEM, not the
    latest lab. Relative intervals are unchanged; the provenance names the
    collection so an admin can see the axis came from narrative timing."""
    case, report = TL.normalize_timeline({
        "lab_panels": [{"panel": "A", "collected_at": "2026-01-31", "results": []}],
        "notes": [{"note_type": "P", "author_role": "x", "collected_at": "2026-02-05", "text": "ok"}],
    })
    assert report["index_source"] == "latest_notes"
    assert case["lab_panels"][0]["collected_offset_days"] == -5
    assert case["notes"][0]["collected_offset_days"] == 0


def test_lab_anchored_charts_keep_their_historical_provenance_name():
    case, report = TL.normalize_timeline({
        "lab_panels": [{"panel": "A", "collected_at": "2025-07-07", "results": []}],
        "notes": [{"note_type": "P", "author_role": "x", "collected_at": "2025-07-01", "text": "ok"}],
    })
    assert report["index_source"] == "latest_observation"


def test_the_no_anchor_hold_says_there_was_no_anchor():
    """No structured date anywhere, dates in the prose: the old message listed
    masked tokens as "unresolved", as if they could not be read. They were never
    tried. The hold now says what an admin can act on."""
    frags = {"notes": [{"note_type": "P", "author_role": "x",
                        "text": "Seen 2025-07-01, again 2025-07-07."}]}
    case, report = TL.normalize_timeline(frags)
    assert report["unresolved"]
    assert report["hold_reason"].startswith("no index anchor: 2 date-like token(s)")
    assert "manifest index_event" in report["hold_reason"]


def test_a_chart_with_no_dates_at_all_has_no_hold_reason():
    case, report = TL.normalize_timeline({"notes": [{"note_type": "P", "author_role": "x", "text": "fine"}]})
    assert report["unresolved"] == [] and "hold_reason" not in report


# ═══════════════════════════════════════════════════════════════════════════════
# §A2 — one bundle, one chart (executed on the committed patient-3 tree)
# ═══════════════════════════════════════════════════════════════════════════════
def _bare_zip(bundle: str) -> bytes:
    """The tree as a hospital posts it: no manifest.json at all."""
    src = zipfile.ZipFile(io.BytesIO(PF.pack_bundle(bundle)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n in src.namelist():
            if n != "manifest.json":
                z.writestr(n, src.read(n))
    return buf.getvalue()


def _adapter_pass(data: bytes):
    """process_upload's adapter → unify → merge steps, without a store."""
    b = ING.unpack_bundle(data)
    manifest = b["manifest"]
    specialty = manifest.get("specialty") or "general"
    per_patient, key_sources = {}, {}
    for e in b["entries"]:
        kind, name = e.get("kind"), e.get("name")
        if kind in ("manifest", "rejected", "unsupported", "dicom"):
            continue
        em = dict(manifest)
        em["filename"] = name
        frag = cf.FORMATS[kind](e["data"], specialty=specialty, manifest=em)
        pk, how = ING._patient_key_and_source(frag, name, manifest)
        key_sources.setdefault(pk, kind if how == "adapter" else how)
        per_patient.setdefault(pk, []).append(frag)
    before = dict(per_patient)
    per_patient, report = ING.unify_patient_keys(per_patient, key_sources, manifest=manifest)
    return manifest, before, per_patient, report


@pytest.fixture(scope="module")
def patient3_bare():
    manifest, before, per_patient, report = _adapter_pass(_bare_zip("patient-3"))
    merged = ING._merge_fragments(next(iter(per_patient.values())))
    body = {k: v for k, v in merged.items() if not str(k).startswith("_")}
    norm, treport = TL.normalize_timeline(
        body, index_event=manifest.get("index_event") or merged.get("_index_event"),
        vitals_at=merged.get("_vitals_at"))
    return {"before": before, "after": per_patient, "report": report,
            "norm": norm, "treport": treport}


def test_patient_three_without_a_manifest_is_one_ingest_case(patient3_bare):
    """Three key sources (FHIR Patient.id, an HL7 PID-3 hash, and 80 unkeyed
    text/CSV files) fold into ONE case, and the report says the unkeyed ones
    were absorbed rather than merely that two keys merged."""
    assert len(patient3_bare["before"]) == 3, sorted(patient3_bare["before"])
    assert "default" in patient3_bare["before"]
    assert len(patient3_bare["after"]) == 1
    rep = patient3_bare["report"]
    assert rep["unified"] is True and rep["into_source"] == "fhir_r4"
    assert rep["unification"] == "single_keyed_patient_absorbed_unkeyed"
    assert rep["unkeyed_fragments_absorbed"] == len(patient3_bare["before"]["default"]) >= 70


def test_two_real_keys_are_still_never_merged():
    """The safety rule §A2 leaves untouched."""
    out, rep = ING.unify_patient_keys(
        {"pat-a": [{}], "pat-b": [{}], "default": [{}]},
        {"pat-a": "fhir_r4", "pat-b": "fhir_r4", "default": "default"})
    assert set(out) == {"pat-a", "pat-b", "default"}
    assert rep["unified"] is False


def test_a_single_keyed_bundle_absorbs_its_unkeyed_files():
    out, rep = ING.unify_patient_keys(
        {"pat-a": [{}], "default": [{}, {}, {}]}, {"pat-a": "fhir_r4", "default": "default"})
    assert list(out) == ["pat-a"] and len(out["pat-a"]) == 4
    assert rep["unkeyed_fragments_absorbed"] == 3


def test_the_fixture_door_declares_the_patient_key_and_specialty():
    names = zipfile.ZipFile(io.BytesIO(PF.pack_bundle("patient-3"))).namelist()
    assert "manifest.json" in names
    import json
    m = json.loads(zipfile.ZipFile(io.BytesIO(PF.pack_bundle("patient-3"))).read("manifest.json"))
    assert m["patient_key"] == "patient-3" and m["specialty"] == "hepatology"


# ═══════════════════════════════════════════════════════════════════════════════
# §A1 + §A3 measured on patient-3: the notes now sit on the axis
# ═══════════════════════════════════════════════════════════════════════════════
def test_patient_three_notes_carry_offsets_and_nothing_is_held(patient3_bare):
    norm, treport = patient3_bare["norm"], patient3_bare["treport"]
    assert treport["unresolved"] == [], treport["unresolved"][:5]
    notes = norm["notes"]
    dated = [n for n in notes if isinstance(n.get("collected_offset_days"), int)]
    # 79 text files (76 dated + 3 compilations/unknown-date) plus the FHIR
    # DocumentReferences. Before §A1: 74 dated, all of them FHIR.
    assert len(dated) >= 140, len(dated)
    assert sum(1 for n in dated if len(n.get("text") or "") >= 200) >= 76
    assert treport["date_order"] == TL.DATE_ORDER_DMY
    # The text notes are no longer 79 "Progress" notes.
    from collections import Counter
    types = Counter(n.get("note_type") for n in notes)
    assert types["Lab report"] >= 50 and types["Radiology"] >= 10 and types["Discharge"] >= 4
    assert types.get("Progress", 0) <= 10


def test_patient_three_clears_the_density_gate_and_yields_a_walk(patient3_bare):
    safe = cf.deidentify(patient3_bare["norm"])
    case = cf.ClinicalCase(**{**safe, "case_source": "real_deid",
                              "specialty": "hepatology"}).model_dump()
    encs = RC.segment_longitudinal_record(case)
    dps = [e for e in encs if RC.qualify_encounter(case, e)["qualifies"]]
    assert len(encs) >= 5 and len(dps) >= 4 and len(RC.pair_decision_points(case, encs)) >= 3
    # Every qualifying encounter now sees BOTH resource types: labs AND notes.
    for e in dps:
        assert set(RC.qualify_encounter(case, e)["resource_types"]) >= {"lab_panels", "notes"}
    summary = ING.content_summary(case)
    assert summary["encounters"] == len(encs) and summary["decision_points"] == len(dps)
    assert summary["specialty_inferred"] in ("hepatology", "nephrology")
    assert 0 < summary["specialty_confidence"] < 1
    assert summary["specialty_floor"] == RC._SPECIALTY_CONFIDENCE_FLOOR


# ═══════════════════════════════════════════════════════════════════════════════
# §A5 / §A6 / §B — the admin surface
# ═══════════════════════════════════════════════════════════════════════════════
def _js(name: str) -> str:
    return (_FRONTEND / name).read_text(encoding="utf-8")


def test_every_catch_in_the_admin_shell_goes_through_errText():
    js = _js("admin_shell.js")
    assert "function errText(e, fallback)" in js
    leaks = [l.strip() for l in js.splitlines()
             if re.search(r"\(e && e\.detail\)\s*\|\|", l) or re.search(r"\be\.detail \|\| e\.message", l)]
    assert not leaks, leaks


def test_errText_renders_an_object_detail_as_its_message(tmp_path):
    """A 422 with ``{error: 'nothing_generatable', blockers: {...}}`` — the shape
    the generate endpoint actually returns — must render its message or its
    error, never ``[object Object]``."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    js = _js("admin_shell.js")
    start = js.index("  function errText(e, fallback)")
    end = js.index("\n  }\n", start) + 4
    script = js[start:end] + """
    console.log(JSON.stringify([
      errText({ detail: { error: 'nothing_generatable', blockers: { 0: ['x'] } }, message: 'Request failed (422)' }, 'fb'),
      errText({ detail: { message: 'Case is quarantined', error: 'state' } }, 'fb'),
      errText({ detail: 'Specialty x is not enabled' }, 'fb'),
      errText({ detail: [{ loc: ['body'], msg: 'field required' }], message: 'Request failed (422)' }, 'fb'),
      errText(new Error('boom'), 'fb'),
      errText(null, 'fb'),
    ]));
    """
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == ["nothing generatable", "Case is quarantined", "Specialty x is not enabled",
                   "Request failed (422)", "boom", "fb"]
    assert "[object Object]" not in json.dumps(got)


def test_box_two_gates_build_on_the_specialty_and_says_why():
    js = _js("admin_shell.js")
    assert "function specialtyGate(u)" in js
    assert "disabled: !mode || !eligible || gate.required" in js
    assert "Set the specialty before building." in js
    # a row ingested before the summary existed is not gated on a measurement
    # nobody took
    assert "const measured = c.specialty_clears_floor === true || c.specialty_clears_floor === false;" in js
    # and the plan modal offers the picker on a specialty-blocked proposal
    assert "specialty not served" in js and "refresh('specialty')" in js


def test_box_one_row_is_three_lines_and_reads_charts_not_cases():
    js = _js("admin_shell.js")
    assert "Bundles that arrived and haven’t been told what they’re for." in js
    assert "' chart' + (charts === 1 ? '' : 's')" in js
    assert "(inferred '" in js
    # the inline irreversibility sentence left the heading; the dialog keeps it
    assert "cannot be undone.'" not in js.split("function askBrokering")[0].split("function paint()")[-1]
    assert "This cannot be undone." in js
    # 260 KB is 260 KB, not "0 MB"
    assert "function humanBytes(n)" in js and "Math.round(u.size_bytes / 1048576)" not in js


def test_partner_leads_renderer_is_gone_and_data_requests_pick_recipients():
    js = _js("admin_health.js")
    assert "renderPartnerLeads" not in js and "/leads/admin" not in js
    assert "Reply by email" not in js
    assert "recipient_hs_ids: chosen" in js
    assert "'Select all'" in js
    for gone in ("ascReqTitle", "ascReqSpecialty", "ascReqCount", "ascReqDue",
                 "Send to every active partner"):
        assert gone not in js, gone


def test_export_screen_is_the_five_scopes_and_one_button():
    js = _js("admin_export.js")
    for gone in ("Licensed to", "Exclusive until", "Export + send to", "Exclusive commitments",
                 "/admin/export/exclusivity", "drawLicence", "refreshCommitments"):
        assert gone not in js, gone
    assert "'Export bundle'" in js
    for scope in ("'case'", "'specialty'", "'version'", "'physician'", "'all'"):
        assert scope in js


def test_the_three_files_parse():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed in this environment")
    for name in ("admin_shell.js", "admin_health.js", "admin_export.js"):
        r = subprocess.run([node, "--check", str(_FRONTEND / name)], capture_output=True, text=True)
        assert r.returncode == 0, (name, r.stderr)


# ═══════════════════════════════════════════════════════════════════════════════
# §B2 — "Unknown sender" and "0 MB"; §B4 — the request body; §A5 — the fixture
# door's second idempotency key
# ═══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def store():
    A.fresh_store()
    from asclepius.store import get_store
    return get_store()


def _active_org(store, name: str, *, state=None, member: bool = True):
    """A health system parked in one onboarding state (default ACTIVE), with one
    approved portal member. Built through the store, as test_hs_data_requests
    does, because this file is about who a request reaches."""
    from asclepius import hs_states
    hs = store.create_health_system_unclaimed(name)
    with store._conn() as conn:
        conn.execute("UPDATE health_systems SET onboarding_state = ? WHERE hs_id = ?",
                     (state or hs_states.ACTIVE, hs["hs_id"]))
    if member:
        store.create_hs_portal_user(
            username=A.uniq(10).lower(), hs_id=hs["hs_id"], password="Pw-" + A.uniq(10),
            email=f"{A.uniq(6)}@{name.split()[0].lower().strip(chr(39))}.example",
            must_reset=False, approval_status="approved")
    return store.get_health_system(hs["hs_id"])



def test_partner_label_resolves_a_health_system_and_never_returns_none(store):
    from routers.asclepius import _partner_label_for_upload

    hs = store.create_health_system_unclaimed("Gray Scrubs Hospitals")
    up = store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs["hs_id"], filename="patient-4.zip",
        sha256="0" * 64, size_bytes=266240, raw_path=None, source_ip=None)
    assert _partner_label_for_upload(store, up) == "Gray Scrubs Hospitals"
    orphan = store.insert_ingest_upload(
        link_id="hs-portal", partner_id="hs-nobody-000000", filename="x.zip",
        sha256="1" * 64, size_bytes=1, raw_path=None, source_ip=None)
    assert _partner_label_for_upload(store, orphan) == "hs-nobody-000000"
    assert _partner_label_for_upload(store, {"partner_id": None, "link_id": None}) == "Unknown sender"


def test_the_uploads_list_emits_the_label_and_an_integer_size(store):
    from fastapi.testclient import TestClient

    client = TestClient(A.app)
    h = A.headers_for(A.make_user(store, role="admin"))
    hs = store.create_health_system_unclaimed("Gray Scrubs Hospitals")
    store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs["hs_id"], filename="patient-4.zip",
        sha256="0" * 64, size_bytes=266240, raw_path=None, source_ip=None)
    store.insert_ingest_upload(
        link_id="hs-portal", partner_id=hs["hs_id"], filename="old.zip",
        sha256="2" * 64, size_bytes=None, raw_path=None, source_ip=None)
    rows = client.get("/api/asclepius/ingestion/uploads?limit=10", headers=h).json()["uploads"]
    by_name = {r["filename"]: r for r in rows}
    assert by_name["patient-4.zip"]["partner_label"] == "Gray Scrubs Hospitals"
    assert by_name["patient-4.zip"]["size_bytes"] == 266240
    assert by_name["old.zip"]["size_bytes"] == 0
    assert all(r["partner_label"] for r in rows)
    assert "content" in by_name["patient-4.zip"] and by_name["patient-4.zip"]["content"]["charts"] == 0


def test_a_message_only_request_is_accepted_and_reaches_only_the_chosen_partners(store, monkeypatch):
    from fastapi.testclient import TestClient

    client = TestClient(A.app)
    h = A.headers_for(A.make_user(store, role="admin"))
    orgs = [_active_org(store, name) for name in ("Gray Scrubs", "St Mary's", "Omics Bank")]
    chosen = [orgs[0]["hs_id"], orgs[1]["hs_id"]]
    r = client.post("/api/asclepius/admin/hs-requests", headers=h,
                    json={"message": "Nephrology CKD cohorts, 2019–2023\nAnything with biopsies.",
                          "recipient_hs_ids": chosen})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipients"] == 2
    req = body["request"]
    assert req["title"] == "Nephrology CKD cohorts, 2019–2023"
    assert req["details"].startswith("Nephrology CKD cohorts")
    assert req["specialty"] == "any" and req["case_count"] == 0
    assert sorted(x["name"] for x in req["recipients"]) == ["Gray Scrubs", "St Mary's"]
    # the outbox holds exactly the chosen organizations
    hs_ids = {row["hs_id"] for row in store.list_hs_request_outbox(req["id"])}
    assert hs_ids == set(chosen)


def test_a_request_with_no_message_and_no_title_is_refused(store):
    from fastapi.testclient import TestClient

    client = TestClient(A.app)
    h = A.headers_for(A.make_user(store, role="admin"))
    r = client.post("/api/asclepius/admin/hs-requests", headers=h, json={"message": "   "})
    assert r.status_code == 400


def test_an_inactive_recipient_is_refused_by_name(store):
    from fastapi.testclient import TestClient

    client = TestClient(A.app)
    h = A.headers_for(A.make_user(store, role="admin"))
    from asclepius import hs_states
    hs = _active_org(store, "Not Yet Signed", state=hs_states.AWAITING_DLA)
    r = client.post("/api/asclepius/admin/hs-requests", headers=h,
                    json={"message": "x", "recipient_hs_ids": [hs["hs_id"]]})
    assert r.status_code == 400 and hs["hs_id"] in r.json()["detail"]


def test_the_structured_request_shape_still_works(store):
    from fastapi.testclient import TestClient

    client = TestClient(A.app)
    h = A.headers_for(A.make_user(store, role="admin"))
    r = client.post("/api/asclepius/admin/hs-requests", headers=h,
                    json={"title": "100 nephrology cases", "specialty": "nephrology",
                          "case_count": 100, "due_date": "2026-10-01", "details": "d"})
    assert r.status_code == 200, r.text
    assert r.json()["request"]["case_count"] == 100


def test_message_only_letter_omits_the_empty_specialty_and_count_rows():
    from onboarding_emails import build_hs_data_request_email

    html = build_hs_data_request_email(title="Nephrology CKD cohorts", specialty_label="Any",
                                       case_count=0, due_date="", details="Anything with biopsies.",
                                       portal_url="https://x.example/provider")
    assert "0 cases" not in html and "Specialty" not in html
    assert "Anything with biopsies." in html
    full = build_hs_data_request_email(title="t", specialty_label="Nephrology", case_count=100,
                                       due_date="2026-10-01", details="", portal_url="https://x")
    assert "100 cases" in full and "Nephrology" in full


def test_a_repacked_fixture_is_not_ingested_twice(store, monkeypatch, tmp_path):
    """Changing a bundle's declared specialty changes the packed bytes and so the
    sha256; the name catches what the digest cannot."""
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("DATA_ENCRYPTION_KEY",
                       __import__("base64").urlsafe_b64encode(b"front-door-test-key-0123456789ab").decode())
    first = PF.ingest_committed_bundles(store, actor="t", bundles=["patient-3"], on_ingested=lambda *a: None)
    assert first["ingested"] == 1
    from asclepius import v4_cases as V4
    monkeypatch.setitem(V4.FIXTURE_BUNDLE_SPECIALTIES, "patient-3", "nephrology")
    again = PF.ingest_committed_bundles(store, actor="t", bundles=["patient-3"], on_ingested=lambda *a: None)
    assert again["ingested"] == 0 and again["skipped"] == 1
    assert "earlier packing" in again["bundles"][0]["message"]
    assert again["bundles"][0]["upload_id"] == first["bundles"][0]["upload_id"]
    assert len(store.list_ingest_uploads(limit=10)) == 1
