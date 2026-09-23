"""Patient JSON stays data inside the existing HTML script block."""
import copy
from html.parser import HTMLParser
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from html_json import json_for_html_script
import main
from tests._role_auth import tenant_token


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.in_script = False
        self.fixture_elements = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True
            self.scripts.append("")
        if ("data-synthetic", "fixture") in attrs:
            self.fixture_elements.append(tag)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts[-1] += data


def test_script_json_round_trips_html_delimiters_unicode_and_line_separators():
    value = {"name": "</ScRiPt><template data-synthetic='fixture'>", "text": "A&B > C – 李\u2028\u2029"}
    encoded = json_for_html_script(value)
    assert not any(char in encoded for char in "<>&\u2028\u2029")
    assert json.loads(encoded) == value


@pytest.mark.parametrize("path", ["/patient/{pid}", "/patient/{pid}/pre-op",
                                 "/patient/{pid}/digital-care-companion", "/patient/{pid}/voice",
                                 "/doctor/patient/{pid}"])
def test_every_patient_page_keeps_stored_name_inside_its_json_script(monkeypatch, path):
    pid = "synthetic-html-" + uuid.uuid4().hex
    name = "</script><template data-synthetic='fixture'>A & B</template>"
    record = {"name": name, "health_system_id": "html-fixture", "pipeline_type": "post_op",
              "structured_data": {"procedure_name": "Synthetic procedure", "procedure_date": "2099-01-01"},
              "voice_audio_url": None, "avatar_url": None,
              "resources": {"diagnosis": {"battlecard_html": "<p>Clinical text & context</p>"},
                            "treatment": {"battlecard_html": "<p>Care instructions</p>"}}}
    original = copy.deepcopy(record)
    monkeypatch.setitem(main.app.state.patient_store, pid, record)
    headers = {"Authorization": "Bearer " + tenant_token(health_system_id="html-fixture")}
    response = TestClient(main.app).get(path.format(pid=pid), headers=headers)
    assert response.status_code == 200, response.text
    parsed = Scripts()
    parsed.feed(response.text)
    assert parsed.fixture_elements == []
    injected = [script for script in parsed.scripts if script.startswith("window.__PATIENT__ = ")]
    assert len(injected) == 1
    data = json.loads(injected[0].removeprefix("window.__PATIENT__ = ").removesuffix(";"))
    assert data["name"] == name
    assert record == original
