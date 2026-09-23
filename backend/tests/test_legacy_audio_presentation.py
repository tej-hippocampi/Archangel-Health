"""Authorized reads preserve and safely present preexisting synthetic audio."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import audio_presentation as presentation
import audio_storage
import main
import patient_session
import realm
from tests._role_auth import tenant_token


@pytest.fixture
def audio_world(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    public = tmp_path / "public"
    legacy.mkdir()
    public.mkdir()
    monkeypatch.setattr(presentation, "LEGACY_AUDIO_ROOT", legacy)
    monkeypatch.setattr(presentation, "audio_root", lambda: public)
    monkeypatch.setattr(audio_storage, "audio_root", lambda: public)
    monkeypatch.setattr(presentation, "_MIGRATED", presentation.OrderedDict())
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "team.db"))
    monkeypatch.setenv("ENFORCE_PATIENT_AUTH", "1")
    monkeypatch.setattr(main, "_persist_demo_patient_store", lambda: None)
    audio_mount = next(route for route in main.app.routes if getattr(route, "path", None) == "/audio")
    monkeypatch.setattr(audio_mount.app, "directory", str(public))
    monkeypatch.setattr(audio_mount.app, "all_directories", [str(public)])
    pid = "synthetic-retained"
    resources = {}
    for suffix in ("", "_diagnosis", "_treatment", "_preop", "_postop"):
        url = f"/audio/audio_{pid}{suffix}.mp3"
        (legacy / url.rsplit("/", 1)[1]).write_bytes(b"ID3 synthetic retained recording " + suffix.encode())
        if suffix:
            resources[suffix[1:]] = {"voice_audio_url": url, "battlecard_html": "<p>Synthetic card</p>"}
    record = {"name": "Synthetic Patient", "health_system_id": "audio-owner", "pipeline_type": "post_op",
              "structured_data": {"procedure_name": "Synthetic procedure"}, "avatar_url": None,
              "voice_audio_url": f"/audio/audio_{pid}.mp3", "resources": resources}
    monkeypatch.setitem(main._patient_store, pid, record)
    headers = {"Authorization": "Bearer " + tenant_token(health_system_id="audio-owner")}
    return TestClient(main.app), pid, record, headers, legacy, public


def urls(value):
    if isinstance(value, dict):
        return [url for key, item in value.items() for url in
                ([item] if key in {"voice_audio_url", "audioUrl", "audio_url"} and item else urls(item))]
    if isinstance(value, list):
        return [url for item in value for url in urls(item)]
    return []


ROUTES = ["/doctor/patient/{pid}", "/patient/{pid}", "/patient/{pid}/pre-op",
          "/api/patient/{pid}/config", "/api/patient/{pid}/resources",
          "/api/patient/{pid}/audio", "/api/patient/{pid}/preop-audio"]


@pytest.mark.parametrize("path", ROUTES)
def test_authorized_response_exposes_playable_opaque_audio_without_rewriting_sources(audio_world, path):
    client, pid, record, headers, legacy, public = audio_world
    original_record = copy.deepcopy(record)
    original_bytes = {p.name: p.read_bytes() for p in legacy.iterdir()}
    response = client.get(path.format(pid=pid), headers=headers)
    assert response.status_code == 200
    if response.headers["content-type"].startswith("text/html"):
        payload = json.loads(re.search(r"window\.__PATIENT__ = (.*?);</script>", response.text).group(1))
    else:
        payload = response.json()
    ready = urls(payload)
    assert ready
    for url in ready:
        assert re.fullmatch(r"/audio/[a-f0-9]{32}\.mp3", url)
        result = client.get(url)
        assert result.status_code == 200
        assert result.content in original_bytes.values()
    assert record == original_record
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == original_bytes
    assert client.get(record["voice_audio_url"]).status_code == 404
    assert all(re.fullmatch(r"[a-f0-9]{32}\.mp3", p.name) for p in public.iterdir())


@pytest.mark.parametrize("path", ROUTES)
def test_denied_request_never_copies_legacy_audio(audio_world, path):
    client, pid, record, _, legacy, public = audio_world
    original = copy.deepcopy(record)
    for headers in ({}, {"Authorization": "Bearer " + tenant_token(health_system_id="foreign-owner")}):
        response = client.get(path.format(pid=pid), headers=headers, follow_redirects=False)
        assert response.status_code in {302, 401, 404}
        assert list(public.iterdir()) == []
    assert record == original


def test_patient_cookie_receives_migrated_audio(audio_world):
    client, pid, _, _, _, _ = audio_world
    client.cookies.set(patient_session.patient_cookie_name(), patient_session.create_patient_session(pid, "audio-owner"))
    response = client.get(f"/api/patient/{pid}/config")
    assert response.status_code == 200
    assert client.get(response.json()["audioUrl"]).status_code == 200


@pytest.mark.parametrize("url", ["/audio/../audio_synthetic-retained.mp3",
                                "/audio/audio_foreign-patient.mp3",
                                "/audio/audio_synthetic-retained.mp3?download=1",
                                "/audio/audio_synthetic-retained%2f.mp3",
                                "/audio/audio_synthetic-retained_arbitrary.mp3"])
def test_only_exact_patient_bound_filenames_can_be_copied(audio_world, url):
    _, pid, _, _, _, public = audio_world
    assert presentation.present_audio_url(url, pid) is None
    assert list(public.iterdir()) == []


def test_symlink_and_sandbox_legacy_sources_are_not_copied(audio_world):
    _, pid, record, _, legacy, public = audio_world
    source = legacy / f"audio_{pid}.mp3"
    source.rename(legacy / "retained-original.mp3")
    source.symlink_to(legacy / "retained-original.mp3")
    assert presentation.present_audio_url(record["voice_audio_url"], pid) is None
    with realm.scoped("sandbox"):
        assert presentation.present_audio_url(f"/audio/audio_{pid}_preop.mp3", pid) is None
    assert list(public.iterdir()) == []
    assert (legacy / "retained-original.mp3").read_bytes().startswith(b"ID3")


def test_parallel_reads_reuse_one_complete_copy(audio_world):
    _, pid, record, _, legacy, public = audio_world
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: presentation.present_audio_url(record["voice_audio_url"], pid), range(8)))
    assert len(set(results)) == 1 and results[0]
    assert len(list(public.iterdir())) == 1
    assert next(public.iterdir()).read_bytes() == (legacy / f"audio_{pid}.mp3").read_bytes()


def test_copy_failure_and_destination_collision_keep_all_existing_bytes(audio_world, monkeypatch):
    _, pid, record, _, legacy, public = audio_world
    original = (legacy / f"audio_{pid}.mp3").read_bytes()
    name = "a" * 32
    monkeypatch.setattr(presentation.uuid, "uuid4", lambda: SimpleNamespace(hex=name))
    target = public / f"{name}.mp3"
    target.write_bytes(b"Existing generated audio")
    assert presentation.present_audio_url(record["voice_audio_url"], pid) is None
    assert target.read_bytes() == b"Existing generated audio"
    monkeypatch.setattr(presentation.uuid, "uuid4", lambda: SimpleNamespace(hex="b" * 32))
    def full_disk(_):
        raise OSError("Synthetic full disk")
    monkeypatch.setattr(presentation.os, "fsync", full_disk)
    assert presentation.present_audio_url(record["voice_audio_url"], pid) is None
    assert list(public.iterdir()) == [target]
    assert (legacy / f"audio_{pid}.mp3").read_bytes() == original


def test_external_audio_urls_remain_unchanged_without_copying(audio_world):
    _, pid, _, _, _, public = audio_world
    url = "https://media.example.org/synthetic-recording.mp3?token=retained"
    assert presentation.audio_presentation({"voice_audio_url": url}, pid) == {"voice_audio_url": url}
    assert list(public.iterdir()) == []
