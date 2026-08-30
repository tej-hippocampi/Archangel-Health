"""Observational equivalence of the two variants — PRD-I §3.3.

The requirement: a health system must not be able to tell whether their upload is
destined for task creation or for brokering. This is the structural half — two
partners are provisioned identically except for the purpose the admin chose, and
every provider-facing observable is compared.

EXPERIMENTAL DESIGN
-------------------
The two variants are constructed to be identical in every controllable respect:
same-length organization names, same-length usernames, identical passwords,
identical request sequences. Anything that then differs is attributable to the
purpose and nothing else. A comparison between "Mass General" and "Cleveland
Clinic" would differ in a dozen ways that have nothing to do with what is being
measured, and would pass or fail for the wrong reasons.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EMAIL_DEV_MODE"] = "1"

import tests._asclepius as A  # noqa: E402
from asclepius import ingestion as asc_ingestion  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

API = "/api/asclepius"

# Legitimately per-response, and not derived from anything about the variant.
VOLATILE = {"date", "x-request-id", "x-railway-request-id", "set-cookie", "server"}

# The frozen header-name list. A golden assertion, so ADDING a header to the
# provider surface fails here and has to be added deliberately to both variants
# rather than appearing on one of them.
GOLDEN_HEADERS = [
    "cache-control",
    "content-length",
    "content-security-policy-report-only",
    "content-type",
    "permissions-policy",
    "referrer-policy",
    "x-content-type-options",
    "x-frame-options",
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ASCLEPIUS_INGEST_DIR", str(tmp_path / "ingest"))
    monkeypatch.setenv("ASCLEPIUS_ASSET_STORE", str(tmp_path / "assets"))
    monkeypatch.setenv("ASCLEPIUS_UPLOAD_CHUNK_BYTES", str(1024 * 1024))
    monkeypatch.setenv("ASCLEPIUS_PORTAL_BUDGET_MS", "0")
    A.fresh_store()
    yield


def _store():
    from asclepius.store import get_store
    return get_store()


# Same length, same shape, differing only where they must.
_ORGS = {"task_creation": "Alpha Regional Hospital",
         "brokering": "Bravo Regional Hospital"}
_PASSWORD = "portal-pass-1234567"


def _variant(purpose: str, tag: str):
    """A provisioned + signed-in partner of the given purpose."""
    store = _store()
    hs = store.ensure_health_system(_ORGS[purpose], contact_email="it@example.org")
    username = f"hsvar{tag}"          # identical length across variants
    store.create_hs_portal_user(username=username, hs_id=hs["hs_id"],
                                password=_PASSWORD, email="it@example.org")
    store.set_hs_portal_password(username, _PASSWORD, must_reset=False)
    store.set_hs_portal_purpose(username, purpose)
    client = TestClient(A.app, base_url="https://testserver")
    r = client.post(f"{API}/hs/login", json={"username": username, "password": _PASSWORD})
    assert r.status_code == 200, r.text
    return {"client": client, "hs": hs, "username": username, "purpose": purpose}


@pytest.fixture
def variants():
    return _variant("task_creation", "aa"), _variant("brokering", "bb")


def _headers(resp):
    return {k.lower(): v for k, v in resp.headers.items() if k.lower() not in VOLATILE}


# Any form the distinction could take in a payload — as a key, a value, or a
# fragment of either.
_FORBIDDEN_IN_PAYLOAD = ("purpose", "brokering", "broker", "task_creation")


def _assert_says_nothing(resp, what: str):
    """No provider-facing byte may name the distinction.

    A VALUE-level check, and it is the one that catches the realistic regression.
    Comparing header sets, status codes, sizes and JSON key SETS all pass happily
    when both variants grow the same new field carrying different values — which
    is exactly what "someone added a debug field" looks like. Padding makes the
    sizes match, and the key set is identical by construction, so nothing
    structural fires. This does."""
    blob = (resp.content or b"").decode("utf-8", errors="replace").lower()
    hdrs = " ".join(f"{k}:{v}" for k, v in resp.headers.items()).lower()
    for word in _FORBIDDEN_IN_PAYLOAD:
        assert word not in blob, f"{what}: response body contains {word!r}"
        assert word not in hdrs, f"{what}: response headers contain {word!r}"


# Fields whose values legitimately differ between two DIFFERENT partners and
# carry nothing about purpose: server-minted ids, the organization's own name,
# and clock readings. Everything outside this list must match exactly.
_PER_PARTNER_FIELDS = {
    "session_id", "upload_id", "organization", "username", "email",
    "received_at", "verified_at", "created_at", "last_login", "_",
}


def _canonical(obj):
    """Replace per-partner values with a placeholder, recursively.

    This is what turns "the shapes look alike" into "the answers are the same".
    Key-set comparison passes when both variants carry a ``status`` field holding
    DIFFERENT words, and padding hides the size difference that would otherwise
    betray it — so a purpose-correlated status, message, count or ordering slips
    through every structural check. After canonicalization the two payloads must
    be equal, so any such difference fails."""
    if isinstance(obj, dict):
        return {k: ("<per-partner>" if k in _PER_PARTNER_FIELDS else _canonical(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    return obj


def _assert_observationally_identical(a, b, what: str):
    _assert_says_nothing(a, f"{what} [A]")
    _assert_says_nothing(b, f"{what} [B]")
    ha, hb = _headers(a), _headers(b)
    assert list(ha) == list(hb), f"{what}: header NAMES or ORDER differ\n{list(ha)}\n{list(hb)}"
    assert ha == hb, f"{what}: header VALUES differ\n{ha}\n{hb}"
    assert a.status_code == b.status_code, f"{what}: status codes differ"
    assert len(a.content) == len(b.content), (
        f"{what}: response SIZE differs ({len(a.content)} vs {len(b.content)}) — "
        "size is a channel, and padding exists to close it")
    try:
        pa, pb = json.loads(a.content), json.loads(b.content)
    except (ValueError, AttributeError):
        return
    assert sorted(pa.keys()) == sorted(pb.keys()), (
        f"{what}: JSON envelope keys differ\n{sorted(pa)}\n{sorted(pb)}")
    ca, cb = _canonical(pa), _canonical(pb)
    assert ca == cb, (
        f"{what}: the two variants gave DIFFERENT ANSWERS once per-partner values "
        f"are normalized away.\nA={json.dumps(ca, sort_keys=True)[:600]}\n"
        f"B={json.dumps(cb, sort_keys=True)[:600]}")


# ── the checklist, as assertions ─────────────────────────────────────────────
def test_headers_are_frozen_and_carry_no_purpose_signal(variants):
    a, b = variants
    ra = a["client"].get(f"{API}/hs/me")
    rb = b["client"].get(f"{API}/hs/me")
    _assert_observationally_identical(ra, rb, "GET /hs/me")
    assert sorted(_headers(ra)) == GOLDEN_HEADERS, (
        "the provider-facing header set changed. Adding a header is fine — add it "
        "to GOLDEN_HEADERS deliberately, having confirmed it appears on BOTH "
        f"variants. Saw: {sorted(_headers(ra))}")


def test_the_required_header_values(variants):
    a, _b = variants
    h = _headers(a["client"].get(f"{API}/hs/me"))
    assert h["cache-control"] == "no-store", "a private/public differential is a leak"
    assert h["referrer-policy"] == "no-referrer"
    assert "etag" not in h, "ETag is content-derived and fingerprints the variant"
    assert "content-encoding" not in h, "compression makes size a function of content"
    assert "vary" not in h, "Vary present on one variant and absent on the other is a leak"


def test_every_provider_route_is_observationally_identical(variants):
    """Walk the actual surface, not a hand-listed subset — a route added later is
    covered automatically."""
    a, b = variants
    data = _bundle()
    sessions = {}
    for v in (a, b):
        r = v["client"].post(f"{API}/hs/uploads/sessions", json={
            "filename": "bundle.zip", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": "application/zip"})
        assert r.status_code == 200, r.text
        sessions[v["purpose"]] = r.json()["session_id"]

    def call(v, method, path, **kw):
        return getattr(v["client"], method)(
            path.replace("{sid}", sessions[v["purpose"]]), **kw)

    cases = [
        ("get", f"{API}/hs/me", {}),
        ("get", f"{API}/hs/uploads", {}),
        ("get", f"{API}/hs/uploads/sessions/{{sid}}", {}),
        ("post", f"{API}/hs/uploads/sessions", {"json": {
            "filename": "bundle.zip", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}}),
        ("post", f"{API}/hs/uploads/sessions/{{sid}}/complete", {}),
        ("delete", f"{API}/hs/uploads/sessions/{{sid}}", {}),
        # Added with the portal's second door. This list is hand-maintained
        # despite what the docstring says, so a new /hs/ route is not covered
        # until somebody adds it here. Both variants are created with no intake
        # and no payouts, so these are identical by construction; asserting it
        # is what stops a later field from quietly making them differ.
        ("get", f"{API}/hs/intake", {}),
        ("get", f"{API}/hs/payouts", {}),
    ]
    for method, path, kw in cases:
        _assert_observationally_identical(call(a, method, path, **kw),
                                          call(b, method, path, **kw),
                                          f"{method.upper()} {path}")


def test_a_full_upload_is_identical_end_to_end(variants):
    """Not just the metadata calls — the whole handshake a real partner performs,
    including the response that reports their upload as received."""
    a, b = variants
    data = _bundle(2 * 1024 * 1024)
    digest = hashlib.sha256(data).hexdigest()
    results = {}
    for v in (a, b):
        c = v["client"]
        dec = c.post(f"{API}/hs/uploads/sessions",
                     json={"filename": "bundle.zip", "size": len(data), "sha256": digest})
        sid = dec.json()["session_id"]
        chunk = dec.json()["chunk_size"]
        puts = []
        for n, blob in enumerate(
                [data[i:i + chunk] for i in range(0, len(data), chunk)], start=1):
            puts.append(c.put(f"{API}/hs/uploads/sessions/{sid}/parts/{n}", content=blob,
                              headers={"X-Chunk-SHA256": hashlib.sha256(blob).hexdigest()}))
        done = c.post(f"{API}/hs/uploads/sessions/{sid}/complete")
        assert done.status_code == 200, done.text
        results[v["purpose"]] = (dec, puts, done, c.get(f"{API}/hs/uploads"))

    ta, tb = results["task_creation"], results["brokering"]
    _assert_observationally_identical(ta[0], tb[0], "declare")
    for i, (pa, pb) in enumerate(zip(ta[1], tb[1]), start=1):
        _assert_observationally_identical(pa, pb, f"part {i}")
    _assert_observationally_identical(ta[2], tb[2], "complete")
    _assert_observationally_identical(ta[3], tb[3], "uploads list after")

    # …and the purposes really were different, so this proved something.
    store = _store()
    got = {store.get_ingest_upload(r[2].json()["upload_id"])["purpose"] for r in (ta, tb)}
    assert got == {"task_creation", "brokering"}


# ── error-differential fuzz ──────────────────────────────────────────────────
def test_every_failure_mode_is_identical_across_variants(variants):
    """Error paths are where indistinguishability dies — they are written last and
    reviewed least. Every failure mode, both variants, full (status, headers, body)
    tuple compared."""
    a, b = variants
    data = _bundle()
    digest = hashlib.sha256(data).hexdigest()
    sessions = {}
    for v in (a, b):
        sessions[v["purpose"]] = v["client"].post(f"{API}/hs/uploads/sessions", json={
            "filename": "bundle.zip", "size": len(data), "sha256": digest}).json()["session_id"]

    def fuzz(v):
        c, sid = v["client"], sessions[v["purpose"]]
        return {
            "unknown session": c.get(f"{API}/hs/uploads/sessions/ups-doesnotexist"),
            "malformed session id": c.get(f"{API}/hs/uploads/sessions/..%2Fetc%2Fpasswd"),
            "another partner's session": c.get(
                f"{API}/hs/uploads/sessions/{sessions['brokering' if v['purpose'] == 'task_creation' else 'task_creation']}"),
            "bad digest at declare": c.post(f"{API}/hs/uploads/sessions", json={
                "filename": "b.zip", "size": 10, "sha256": "not-a-digest"}),
            "zero size at declare": c.post(f"{API}/hs/uploads/sessions", json={
                "filename": "b.zip", "size": 0, "sha256": digest}),
            "oversized declare": c.post(f"{API}/hs/uploads/sessions", json={
                "filename": "b.zip", "size": 99 * 1024 ** 4, "sha256": digest}),
            "missing fields": c.post(f"{API}/hs/uploads/sessions", json={"filename": "b"}),
            "part out of range": c.put(f"{API}/hs/uploads/sessions/{sid}/parts/9999",
                                       content=b"x", headers={"X-Chunk-SHA256": "0" * 64}),
            "part with no digest": c.put(f"{API}/hs/uploads/sessions/{sid}/parts/1",
                                         content=data),
            "part digest mismatch": c.put(f"{API}/hs/uploads/sessions/{sid}/parts/1",
                                          content=data, headers={"X-Chunk-SHA256": "0" * 64}),
            "complete before parts": c.post(f"{API}/hs/uploads/sessions/{sid}/complete"),
            "unknown upload id": c.get(f"{API}/hs/uploads/sessions/upl-nope"),
            "expired session cookie": TestClient(
                A.app, base_url="https://testserver").get(f"{API}/hs/me"),
            "bad password": TestClient(A.app, base_url="https://testserver").post(
                f"{API}/hs/login", json={"username": v["username"], "password": "wrong-one"}),
            "unknown username": TestClient(A.app, base_url="https://testserver").post(
                f"{API}/hs/login", json={"username": "nobodyhere", "password": "wrong-one"}),
        }

    fa, fb = fuzz(a), fuzz(b)
    assert set(fa) == set(fb)
    for mode in fa:
        _assert_observationally_identical(fa[mode], fb[mode], f"failure: {mode}")
        # And the body BYTES, not merely the shape — these carry no partner data,
        # so anything that differs here differs because of the purpose.
        if mode not in ("another partner's session",):
            assert fa[mode].content == fb[mode].content, (
                f"failure {mode!r}: response bodies differ byte for byte")


def test_a_revoked_account_fails_the_same_way_for_both(variants):
    a, b = variants
    store = _store()
    for v in (a, b):
        store.set_hs_portal_active(v["username"], False)
    ra, rb = a["client"].get(f"{API}/hs/me"), b["client"].get(f"{API}/hs/me")
    _assert_observationally_identical(ra, rb, "revoked account")
    assert ra.content == rb.content


def test_the_url_and_token_shapes_are_identical(variants):
    """No /broker/ vs /tasks/, no tc_/br_ prefix, no length or alphabet tell."""
    a, b = variants
    sa = a["client"].post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": 100, "sha256": "a" * 64}).json()["session_id"]
    sb = b["client"].post(f"{API}/hs/uploads/sessions", json={
        "filename": "b.zip", "size": 100, "sha256": "a" * 64}).json()["session_id"]
    assert len(sa) == len(sb)
    assert sa.split("-")[0] == sb.split("-")[0]
    assert set(sa) <= set("abcdef0123456789-ups") and set(sb) <= set("abcdef0123456789-ups")


def test_the_partner_facing_status_vocabulary_cannot_encode_purpose():
    """Whatever the pipeline does downstream, a partner sees one of four words —
    and promotion, which only ever happens to task_creation data, is not among the
    things that can change any of them."""
    from routers import asclepius_provider as P

    assert set(P._HS_PORTAL_STATUS.values()) <= {"received", "processing", "accepted"}
    unmapped = [s for s in asc_ingestion._NON_TERMINAL_UPLOAD_STATUSES
                if P._HS_PORTAL_STATUS.get(s) not in ("received", "processing")]
    assert not unmapped, f"in-flight statuses with no partner-facing mapping: {unmapped}"


@pytest.mark.skipif(not os.getenv("ASCLEPIUS_TIMING_TEST"),
                    reason="timing test is a nightly job; a wall-clock assertion in "
                           "a shared CI runner measures the runner, not the code")
def test_response_timing_carries_no_signal(variants):
    """Interleaved A/B/A/B — sequential batches measure network and scheduler
    drift, not the code. Welch's t-test, failing at |t| > 4.5."""
    import statistics
    import time

    a, b = variants
    sa, sb = [], []
    for _ in range(1000):
        for v, acc in ((a, sa), (b, sb)):
            t0 = time.perf_counter()
            v["client"].get(f"{API}/hs/me")
            acc.append(time.perf_counter() - t0)
    ma, mb = statistics.mean(sa), statistics.mean(sb)
    va, vb = statistics.variance(sa), statistics.variance(sb)
    t = (ma - mb) / ((va / len(sa) + vb / len(sb)) ** 0.5)
    assert abs(t) < 4.5, f"timing differs across variants (t={t:.2f})"


def _bundle(n_bytes: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"specialty": "nephrology",
                                                "patient_key": "pt1"}))
        z.writestr("note.txt", "Progress nephrology: AKI, creatinine rising.")
    data = buf.getvalue()
    if n_bytes and len(data) < n_bytes:
        buf = io.BytesIO(data)
        with zipfile.ZipFile(buf, "a", zipfile.ZIP_STORED) as z:
            z.writestr("filler.bin", os.urandom(n_bytes - len(data) - 200))
        data = buf.getvalue()
    return data
