"""Profile pictures.

Three of the decisions here are security decisions and these are the tests that
hold them.

The bytes decide the type, never the header: the stored blob is served
``inline`` from the app origin to an admin whose bearer token lives in
localStorage, so trusting a declared Content-Type on the way in is how an
upload becomes stored XSS. An SVG named ``headshot.png`` is the whole attack.

The re-encode is not an optimisation: a phone photograph carries GPS
coordinates in EXIF, and a physician uploading a selfie has no idea they are
also uploading where they took it.

And a picture is not a credential. Setting one must not touch verification,
tier or score, and must work while a physician is still under review, because
they are in the community from the day they sign up.
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from asclepius import avatar as asc_avatar
from tests._asclepius import app, fresh_store, headers_for, make_user

client = TestClient(app)

PIL = pytest.importorskip("PIL", reason="Pillow drives every strip/crop path here")


def _png(size=(64, 64), color=(20, 160, 60)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_gps(size=(64, 64)) -> bytes:
    """A JPEG carrying EXIF, GPS included. This is what a phone produces.

    Built with Pillow's own EXIF writer rather than piexif so this test can
    never silently skip. The guarantee it holds -- that a physician's selfie
    does not also upload where they took it -- is the one worth never letting
    lapse to "1 skipped" in a green run.
    """
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational

    im = Image.new("RGB", size, (200, 40, 40))
    exif = Image.Exif()
    exif[0x010F] = "Apple"          # Make
    exif[0x0110] = "iPhone 15 Pro"  # Model
    # GPSInfo. Rationals have to be IFDRational: a bare (num, den) tuple makes
    # Pillow's writer raise on abs().
    exif[0x8825] = {
        1: "N",
        2: (IFDRational(37, 1), IFDRational(52, 1), IFDRational(0, 1)),
        3: "W",
        4: (IFDRational(122, 1), IFDRational(16, 1), IFDRational(0, 1)),
    }
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture
def doctor():
    store = fresh_store()
    user = make_user(store, role="evaluator", tier="labeler", specialty="nephrology")
    with store._conn() as conn:
        conn.execute("UPDATE users SET full_name = ? WHERE id = ?",
                     ("Ahmed Al Otaibi", user["id"]))
    return store, store.get_user_by_id(user["id"])


# ─── The bytes decide ────────────────────────────────────────────────────────
@pytest.mark.parametrize("payload,label", [
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "svg"),
    (b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", "pdf"),
    (b"<!DOCTYPE html><html><body>hi</body></html>", "html"),
    (b"GIF89a\x01\x00\x01\x00", "gif"),
    (b"PK\x03\x04zip", "zip"),
    (b"just some text", "text"),
])
def test_a_non_image_is_refused_on_its_bytes_not_its_name(doctor, payload, label):
    """Every one of these is uploaded as "headshot.png" with an image
    Content-Type, which is exactly how the real attempt would arrive. SVG is
    the one that matters: it is a document that can carry script, and it is
    what every filename-based image check lets through."""
    store, user = doctor
    r = client.post(
        "/api/asclepius/me/avatar",
        files={"file": ("headshot.png", payload, "image/png")},
        headers=headers_for(user),
    )
    assert r.status_code == 422, f"{label} was accepted"
    assert store.get_user_by_id(user["id"])["avatar_asset_sha"] is None


def test_a_real_png_is_accepted(doctor):
    store, user = doctor
    r = client.post(
        "/api/asclepius/me/avatar",
        # Declared as a JPEG on purpose: the bytes are a PNG and the bytes win.
        files={"file": ("me.jpg", _png(), "image/jpeg")},
        headers=headers_for(user),
    )
    assert r.status_code == 200, r.text
    assert r.json()["avatar"]["url"]
    assert store.get_user_by_id(user["id"])["avatar_asset_sha"]


# ─── Metadata never survives ─────────────────────────────────────────────────
def test_exif_and_gps_do_not_survive_the_upload():
    """The load-bearing one. A physician uploading a selfie does not know it
    carries the coordinates of wherever they took it."""
    from PIL import Image

    original = _jpeg_with_gps()
    before = Image.open(io.BytesIO(original)).getexif()
    assert before.get(0x8825) or before.get_ifd(0x8825), (
        "the fixture itself carries no GPS, so this proves nothing")

    clean, mime, _sha = asc_avatar.process_avatar(original)

    after = Image.open(io.BytesIO(clean)).getexif()
    assert not after.get_ifd(0x8825), "GPS survived the re-encode"
    assert not after.get(0x010F) and not after.get(0x0110), "camera make/model survived"
    # Belt and braces: the strings themselves are gone from the bytes, so this
    # holds even if a future Pillow reads EXIF differently than it writes it.
    assert b"Apple" not in clean and b"iPhone" not in clean


def test_a_wide_image_is_cropped_square():
    """Cropped server-side so the stored bytes are what everyone sees. Left to
    CSS, a 4000x600 panorama renders as a face in one place and a sliver in
    another."""
    from PIL import Image
    clean, _mime, _sha = asc_avatar.process_avatar(_png(size=(1200, 300)))
    im = Image.open(io.BytesIO(clean))
    assert im.size[0] == im.size[1]


def test_a_large_image_is_capped(doctor):
    from PIL import Image
    clean, _mime, _sha = asc_avatar.process_avatar(_png(size=(3000, 3000)))
    assert Image.open(io.BytesIO(clean)).size[0] == asc_avatar.AVATAR_DIM


def test_an_oversized_upload_is_refused_before_it_is_decoded(doctor, monkeypatch):
    store, user = doctor
    monkeypatch.setattr(asc_avatar, "avatar_max_bytes", lambda: 1024)
    r = client.post(
        "/api/asclepius/me/avatar",
        files={"file": ("me.png", _png(size=(400, 400)), "image/png")},
        headers=headers_for(user),
    )
    assert r.status_code == 413
    assert "MB" in r.json()["detail"]


# ─── Serving ─────────────────────────────────────────────────────────────────
def test_the_picture_is_served_with_nosniff(doctor):
    store, user = doctor
    client.post("/api/asclepius/me/avatar",
                files={"file": ("me.png", _png(), "image/png")},
                headers=headers_for(user))
    r = client.get(f"/api/asclepius/users/{user['id']}/avatar", headers=headers_for(user))
    assert r.status_code == 200
    # The one header that stops a browser second-guessing the media type we
    # declared. Without it, "inline" plus a creative payload is the whole bug.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-type"].startswith("image/")


def test_a_colleague_can_see_the_picture(doctor):
    """Not self-scoped. The point of a profile picture is that colleagues see
    it beside a message and an admin sees it while checking a registry entry."""
    store, user = doctor
    client.post("/api/asclepius/me/avatar",
                files={"file": ("me.png", _png(), "image/png")},
                headers=headers_for(user))
    colleague = make_user(store, role="evaluator", tier="labeler")
    r = client.get(f"/api/asclepius/users/{user['id']}/avatar",
                   headers=headers_for(colleague))
    assert r.status_code == 200


def test_a_signed_out_request_gets_nothing(doctor):
    store, user = doctor
    client.post("/api/asclepius/me/avatar",
                files={"file": ("me.png", _png(), "image/png")},
                headers=headers_for(user))
    assert client.get(f"/api/asclepius/users/{user['id']}/avatar").status_code == 401


def test_no_picture_is_a_404_so_the_client_falls_back_to_initials(doctor):
    store, user = doctor
    r = client.get(f"/api/asclepius/users/{user['id']}/avatar", headers=headers_for(user))
    assert r.status_code == 404


def test_a_missing_blob_is_a_404_rather_than_a_500(doctor, monkeypatch):
    """An ephemeral asset store wiped by a redeploy. Cosmetic and recoverable:
    the initials come back and they can upload again."""
    store, user = doctor
    client.post("/api/asclepius/me/avatar",
                files={"file": ("me.png", _png(), "image/png")},
                headers=headers_for(user))
    from asclepius import assets as asc_assets

    def _gone(*a, **k):
        raise asc_assets.AssetError("blob missing")

    monkeypatch.setattr("routers.asclepius.asc_assets.load_asset", _gone)
    r = client.get(f"/api/asclepius/users/{user['id']}/avatar", headers=headers_for(user))
    assert r.status_code == 404


# ─── A picture is not a credential ───────────────────────────────────────────
def test_a_physician_awaiting_verification_can_set_one():
    """They are in the community from the day they sign up, and appearing there
    as two grey letters for a week is a poor welcome."""
    store = fresh_store()
    user = make_user(store, role="evaluator", tier=None)
    store.set_verification_status(user["id"], "pending")
    r = client.post("/api/asclepius/me/avatar",
                    files={"file": ("me.png", _png(), "image/png")},
                    headers=headers_for(store.get_user_by_id(user["id"])))
    assert r.status_code == 200


def test_setting_a_picture_changes_nothing_about_standing(doctor):
    store, user = doctor
    before = store.get_user_by_id(user["id"])
    client.post("/api/asclepius/me/avatar",
                files={"file": ("me.png", _png(), "image/png")},
                headers=headers_for(user))
    after = store.get_user_by_id(user["id"])
    for field in ("verification_status", "tier", "tier_score", "npi",
                  "registry_id", "board_cert"):
        assert after[field] == before[field], field


def test_removing_a_picture_clears_all_three_columns(doctor):
    store, user = doctor
    client.post("/api/asclepius/me/avatar",
                files={"file": ("me.png", _png(), "image/png")},
                headers=headers_for(user))
    r = client.delete("/api/asclepius/me/avatar", headers=headers_for(user))
    assert r.status_code == 200
    assert r.json()["avatar"]["url"] is None
    row = store.get_user_by_id(user["id"])
    assert row["avatar_asset_sha"] is None
    assert row["avatar_mime"] is None
    assert row["avatar_updated_at"] is None


# ─── The profile payload ─────────────────────────────────────────────────────
def test_the_profile_carries_initials_and_an_accent_before_any_upload(doctor):
    """The fallback is the physician's initials on their specialty's colour,
    the same two letters their colleagues already see in the community. Not a
    grey silhouette, which reads as "we have no record of you" on the one
    screen whose job is to show that we do."""
    store, user = doctor
    body = client.get("/api/asclepius/me/profile", headers=headers_for(user)).json()
    assert body["avatar"]["url"] is None
    assert body["avatar"]["initials"] == "AO"   # first name + LAST name
    assert body["avatar"]["accent"] == "green"        # nephrology


def test_the_initials_match_the_ones_the_community_shows(doctor):
    """Two implementations of "first letter, last letter" disagree the first
    time somebody has a middle name."""
    from community.router import _initials

    store, user = doctor
    body = client.get("/api/asclepius/me/profile", headers=headers_for(user)).json()
    assert body["avatar"]["initials"] == _initials(user["full_name"])


def test_the_url_changes_when_the_picture_does(doctor):
    """Cache-busted on the content hash. `private, max-age=3600` otherwise
    means a physician who replaces their photo keeps seeing the old one for an
    hour and concludes the upload failed."""
    store, user = doctor
    client.post("/api/asclepius/me/avatar",
                files={"file": ("a.png", _png(color=(10, 10, 200)), "image/png")},
                headers=headers_for(user))
    first = client.get("/api/asclepius/me/profile",
                       headers=headers_for(user)).json()["avatar"]["url"]
    client.post("/api/asclepius/me/avatar",
                files={"file": ("b.png", _png(color=(200, 10, 10)), "image/png")},
                headers=headers_for(user))
    second = client.get("/api/asclepius/me/profile",
                        headers=headers_for(user)).json()["avatar"]["url"]
    assert first and second and first != second


def test_the_self_profile_carries_no_rating_at_all(doctor):
    """REVERSED. This used to assert the band was populated whenever a score
    was, because the profile rendered a bare number with nothing saying what it
    meant. The answer to that turned out to be that a physician should not be
    reading their own contributor score in the first place: it is an instrument
    for routing and for pay, and shown to the person it measures it became a
    number they were managing.

    Still computed, still stored, still read by the admin. Just not shipped on
    the one endpoint whose contract is "everything the portal shows a physician
    about their own account"."""
    store, user = doctor
    store.upsert_contributor_score(user_id=user["id"], score=74.2, n_cases=6,
                                   components={})
    standing = client.get("/api/asclepius/me/profile",
                          headers=headers_for(user)).json()["standing"]
    assert "score" not in standing
    assert "band" not in standing
    # Capability vocabulary stays: it is not a rating, and other things read it.
    assert "tier_word" in standing
    # And the score itself is untouched where it actually lives.
    assert store.get_contributor_score(user["id"])["score"] == 74.2
