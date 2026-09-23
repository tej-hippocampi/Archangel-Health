"""Generated/stored card markup is presentation data, never executable code."""
import asyncio
import copy
from html.parser import HTMLParser
import json
import re
import uuid

from fastapi.testclient import TestClient
import pytest
import tinycss2

from card_html import card_presentation, sanitize_card_html
import main
from patient_session import create_patient_session
from prompts.diagnosis import DIAGNOSIS_BATTLECARD_PROMPT
from prompts.treatment import TREATMENT_BATTLECARD_PROMPT
from tests._role_auth import tenant_token


PAYLOADS = [
    '<script>window.__xss=1</script><p onclick="window.__xss=1">Care</p>',
    '<img src=x onerror="window.__xss=1"><svg onload="window.__xss=1"><path d="M0 0"/></svg>',
    '<a href="jav&#x61;script:window.__xss=1">Care</a>',
    '<iframe srcdoc="&lt;script&gt;parent.__xss=1&lt;/script&gt;"></iframe>',
    '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=window.__xss=1>">',
    '<svg><foreignObject><iframe srcdoc="<script>parent.__xss=1</script>"></iframe></foreignObject></svg>',
    '<svg><a><animate attributeName="href" values="javascript:window.__xss=1"/></a></svg>',
    '<form id="__PATIENT__"><input name="location"></form><a id="location" name="fetch">Care</a>',
    '<style>@import "https://example.invalid/x";.card{background:u\\72l(https://example.invalid/x);color:red}</style>',
    '<p style="background:image-set(\'https://example.invalid/x\' 1x);width:e\\78pression(window.__xss=1);color:red">Care</p>',
    '<svg><path fill="url(https://example.invalid/x)" d="M0 0"/></svg>',
    '<style>.card{background:image("https://example.invalid/x");content:future-fetch("https://example.invalid/x")}</style>',
]


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, text):
        self.text.append(text)


@pytest.mark.parametrize("raw", PAYLOADS)
def test_executable_html_and_network_css_are_removed(raw):
    clean = sanitize_card_html(raw)
    parsed = Markup()
    parsed.feed(clean)
    for tag, attrs in parsed.elements:
        assert tag not in {"script", "iframe", "object", "embed", "form", "input", "img", "animate", "foreignobject"}
        assert not any(key.lower().startswith("on") or key == "name" for key in attrs)
        assert not attrs.get("href", "").lower().startswith("javascript:")
        if "id" in attrs:
            assert attrs["id"].startswith(("care-card-", "tb-anchor-"))
    assert "example.invalid" not in clean
    assert "expression(" not in clean
    assert sanitize_card_html(clean) == clean


def test_layout_links_svg_and_teachback_anchors_remain_usable():
    raw = '''<style>#details,.card{display:grid;grid-template-columns:1fr 2fr;color:#123456}
        @media (max-width:600px){.card{display:block}}</style>
        <section class="card" id="details" style="padding:12px;border-radius:8px;background:linear-gradient(red,blue)">
        <h2 id="tb-anchor-dose">Dose</h2><p>Take 1 &amp; ½ tablets if value &lt; 5.</p>
        <a href="#details">Details</a><a href="#tb-anchor-dose">Dose</a>
        <a href="https://example.org/help" target="_blank">Help</a><a href="tel:5550100">Call</a>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M0 0 L24 24" stroke="currentColor" stroke-width="2"/></svg>
        </section>'''
    clean = sanitize_card_html(raw)
    for retained in ['class="card"', 'id="care-card-details"', '#care-card-details',
                     'id="tb-anchor-dose"', 'href="#tb-anchor-dose"', 'href="#care-card-details"',
                     'rel="noopener noreferrer"', 'href="tel:5550100"', 'viewBox="0 0 24 24"',
                     'grid-template-columns:1fr 2fr', 'linear-gradient(red,blue)', '@media']:
        assert retained in clean
    parsed = Markup(); parsed.feed(clean)
    assert "Take 1 & ½ tablets if value < 5." in parsed.text


@pytest.mark.parametrize("prompt", [DIAGNOSIS_BATTLECARD_PROMPT, TREATMENT_BATTLECARD_PROMPT])
def test_actual_prompt_templates_keep_all_css_declarations_and_markup(prompt):
    raw = prompt[prompt.index("<style>"):]
    clean = sanitize_card_html(raw)
    before = Markup(); before.feed(raw)
    after = Markup(); after.feed(clean)
    # Inline CSS serialization may gain a terminating semicolon, but values
    # and markup/classes must remain the same.
    def normalized(elements):
        return [(tag, {k: (v.rstrip(';') if k == 'style' else v) for k, v in attrs.items()})
                for tag, attrs in elements]
    assert normalized(before.elements) == normalized(after.elements)
    original_css = re.search(r"<style>(.*?)</style>", raw, re.S).group(1)
    clean_css = re.search(r"<style>(.*?)</style>", clean, re.S).group(1)

    def declarations(css):
        return [(tinycss2.serialize(rule.prelude).strip(),
                 [(d.lower_name, tinycss2.serialize(d.value).strip(), d.important)
                  for d in tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
                  if d.type == "declaration"])
                for rule in tinycss2.parse_stylesheet(css, skip_comments=True, skip_whitespace=True)
                if rule.type == "qualified-rule"]

    assert declarations(clean_css) == declarations(original_css)


def test_plain_text_inequalities_and_copy_semantics():
    raw = "Clinical text: 2 < 5, 8 > 3 & continue ± ½ tablet."
    parsed = Markup(); parsed.feed(sanitize_card_html(raw))
    assert "".join(parsed.text) == raw
    source = {"resources": [{"battlecard_html": PAYLOADS[0], "voice_script": raw}], "original": PAYLOADS[0]}
    frozen = copy.deepcopy(source)
    clean = card_presentation(source)
    assert clean["resources"][0]["battlecard_html"] != source["resources"][0]["battlecard_html"]
    assert clean["resources"][0]["voice_script"] == raw
    assert clean["original"] == PAYLOADS[0]
    assert source == frozen


@pytest.fixture
def patient(monkeypatch):
    pid = "synthetic-card-" + uuid.uuid4().hex
    raw = '<style>.card{color:#123456}</style><p class="card" id="tb-anchor-dose" onclick="window.__xss=1">Synthetic care</p>'
    record = {"id": pid, "name": "Synthetic Patient", "health_system_id": "card-fixture",
              "pipeline_type": "post_op", "current_tier": "TIER_1", "structured_data": {},
              "battlecard_html": raw, "voice_audio_url": None, "avatar_url": None,
              "resources": {key: {"voice_script": "Synthetic care.", "battlecard_html": raw}
                            for key in ("diagnosis", "treatment", "preop")}}
    monkeypatch.setitem(main.app.state.patient_store, pid, record)
    headers = {"Authorization": "Bearer " + tenant_token(health_system_id="card-fixture")}
    return pid, record, headers


@pytest.mark.parametrize("suffix", ["resources", "battlecard"])
def test_stored_resource_reads_sanitize_every_track_without_rewriting_originals(patient, monkeypatch, suffix):
    pid, record, headers = patient
    original = copy.deepcopy(record)
    def unexpected_persist():
        pytest.fail("A presentation read must not persist altered source HTML")
    monkeypatch.setattr(main, "_persist_demo_patient_store", unexpected_persist)
    response = TestClient(main.app).get(f"/api/patient/{pid}/{suffix}", headers=headers)
    assert response.status_code == 200, response.text
    assert "onclick" not in response.text
    assert "Synthetic care" in response.text
    assert record == original


@pytest.mark.parametrize("path", ["/patient/{pid}", "/doctor/patient/{pid}", "/patient/{pid}/pre-op"])
def test_injected_patient_page_cards_are_sanitized(patient, path):
    pid, record, headers = patient
    original = copy.deepcopy(record)
    response = TestClient(main.app).get(path.format(pid=pid), headers=headers)
    assert response.status_code == 200, response.text
    injected = re.search(r"window\.__PATIENT__ = ([^\n]+);", response.text)
    assert injected
    data = json.loads(injected.group(1))
    assert "onclick" not in json.dumps(data)
    assert "Synthetic care" in json.dumps(data)
    assert record == original


def test_stream_and_terminal_payloads_are_safe_copies():
    payload = {"resources": {"diagnosis": {"battlecard_html": PAYLOADS[0]}}, "battlecard_html": PAYLOADS[1]}
    frozen = copy.deepcopy(payload)
    async def events():
        yield {"stage": "resource", "payload": payload}
        yield {"stage": "complete", "payload": payload}
    async def consume():
        terminal = await main._collect_stream_payload(events())
        stream = [part async for part in main._sse(events())]
        return terminal, stream
    terminal, stream = asyncio.run(consume())
    for body in [terminal] + [json.loads(part.removeprefix("data: "))["payload"] for part in stream]:
        assert "onclick" not in json.dumps(body)
        assert "onload" not in json.dumps(body)
    assert payload == frozen


def test_teachback_returns_safe_card_but_preserves_generated_original(patient, monkeypatch):
    pid, record, _ = patient
    raw = record["resources"]["diagnosis"]["battlecard_html"]
    async def generate(**kwargs):
        return [{"id": "synthetic-question", "battlecard_anchor": "tb-anchor-dose", "question": "Synthetic care?"}], raw
    monkeypatch.setattr("routers.teachback.generate_teachback_questions", generate)
    client = TestClient(main.app)
    client.cookies.set("pt_session", create_patient_session(pid, "card-fixture"))
    response = client.post(f"/api/episodes/{pid}/teachback/post_op_diagnosis/start", json={})
    assert response.status_code == 200, response.text
    assert "onclick" not in response.json()["battlecard_html"]
    assert 'id="tb-anchor-dose"' in response.json()["battlecard_html"]
    assert record["resources"]["diagnosis"]["battlecard_html"] == raw


def test_css_string_serialization_cannot_create_a_second_unsanitized_style_tag():
    raw = r'''<style>.card{content:"\3c/style\3e\3cstyle\3e @import 'https://example.invalid/x';\3c/style\3e"}</style><p class="card">Care</p>'''
    clean = sanitize_card_html(raw)
    assert clean.count('<style>') == 1
    assert clean.count('</style>') == 1
    assert r'\3c /style' in clean


def test_internal_prompt_preview_is_sanitized(monkeypatch):
    import routers.internal as internal
    monkeypatch.setenv('INTERNAL_TOOL_SECRET', 'synthetic-preview-secret')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-no-network-fixture')
    async def generate(**kwargs):
        return object(), {}
    monkeypatch.setattr(internal, 'call_llm', generate)
    monkeypatch.setattr(internal, 'first_text', lambda value: PAYLOADS[0])
    response = TestClient(main.app).post('/internal/run',
        headers={'Authorization': 'Bearer synthetic-preview-secret'},
        json={'prompt_id': 'diagnosis_battlecard', 'system_prompt': 'Synthetic', 'discharge_notes': 'Synthetic'})
    assert response.status_code == 200, response.text
    assert response.json()['battlecard_html'] == '<p>Care</p>'
