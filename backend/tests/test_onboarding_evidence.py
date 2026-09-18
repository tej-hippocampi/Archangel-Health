"""Evidence provenance/transport regression tests; no live models or services."""
import asyncio
import copy
import hashlib

import httpx
import pytest

from asclepius import onboarding_cases as bank, onboarding_evidence as evidence
from tests.test_onboarding_specialty_cases import SOURCES, approved_review, fixture_entry


def article(*, pmid='123', pmcid='456', license='https://creativecommons.org/licenses/by/4.0/'):
    return f'''<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
      <article-id pub-id-type="pmid">{pmid}</article-id><article-id pub-id-type="pmc">{pmcid}</article-id>
      <permissions><license><license-p><ext-link xlink:href="{license}">Terms</ext-link></license-p></license></permissions>
      </article-meta></front><body><sec><title>Asthma in adults</title>
      <p>Adults with asthma should receive the explicitly described fictional treatment for this software fixture.</p>
      <sec><title>Contraindications</title><p>The fictional asthma treatment must not be used when the contraindication in this paragraph applies.</p></sec>
      <table-wrap><label>Table 1</label><table><tr><td>Asthma population</td><td>Fictional recommendation</td></tr></table></table-wrap>
      </sec></body><back><ref-list><ref>Excluded reference list</ref></ref-list></back></article>'''.encode()


def source():
    return {'id': '123', 'pmcid': 'PMC456', 'abstract': 'An abstract defining the guideline scope, without recommendations.'}


def test_open_text_requires_matching_ids_and_reusable_license():
    for damage in ({'pmid': '999'}, {'pmcid': '999'}, {'license': 'https://creativecommons.org/licenses/by-nc/4.0/'},
                   {'license': 'https://creativecommons.org.evil.test/licenses/by/4.0/'}, {'license': ''}):
        assert evidence.parse_open_text(article(**damage), source(), 'asthma controller treatment') is None
    parsed = evidence.parse_open_text(article(), source(), 'asthma controller treatment')
    assert 'Contraindications' in parsed['body_excerpts']
    assert 'Table 1' in parsed['body_excerpts']
    assert 'Excluded reference list' not in parsed['body_excerpts']
    assert parsed['body_excerpts_sha256'] == hashlib.sha256(parsed['body_excerpts'].encode()).hexdigest()
    assert parsed['body_excerpts_url'].endswith('/PMC456/fullTextXML')
    with pytest.raises(ValueError, match='entity'):
        evidence.parse_open_text(b'<!ENTITY malicious>', source(), 'asthma')


def test_passages_are_literal_and_resolve_without_changing_clinical_judgment():
    entry = fixture_entry()
    original = approved_review(entry)
    original['confidence'] = .42
    original['evidence_supported'] = False
    before = copy.deepcopy(original)
    resolved = bank.resolve_review_passages(original, evidence.passages(SOURCES))
    assert resolved['confidence'] == .42 and resolved['evidence_supported'] is False
    assert original == before  # resolution does not mutate the provider's response
    assert resolved is not original
    with pytest.raises(ValueError, match='clinical_review_failed'):
        bank.validate_review(resolved, entry, SOURCES)
    valid = bank.resolve_review_passages(approved_review(entry), evidence.passages(SOURCES))
    bank.validate_review(valid, entry, SOURCES)


@pytest.mark.parametrize('selection', [['invented:p0'], ['2:p0'], [], '1:p0', [None]])
def test_invented_wrong_source_or_missing_passage_is_rejected(selection):
    review = approved_review(fixture_entry())
    review['claim_checks'][0]['source_passage_ids'] = selection
    with pytest.raises(ValueError, match='invalid_source_passage_selection'):
        bank.resolve_review_passages(review, evidence.passages(SOURCES))


def test_body_quotes_are_revalidated_and_tampering_fails():
    src = source()
    src.update(evidence.parse_open_text(article(), src, 'asthma'))
    for passage in evidence.passages([src]).values():
        assert 16 <= len(passage['quote']) <= 900
        assert passage['quote'] in ' '.join(evidence.source_text(src).split())
    src['body_excerpts'] += 'A fabricated recommendation.'
    with pytest.raises(ValueError, match='checksum mismatch'):
        evidence.passages([src])


def test_current_pmcid_metadata_spelling_is_supported():
    raw = article().replace(b'pub-id-type="pmc">456', b'pub-id-type="pmcid">PMC456')
    assert evidence.parse_open_text(raw, source(), 'asthma')['body_excerpts']


def test_long_sources_are_split_without_ellipses_or_rewritten_punctuation():
    text = 'A source sentence with a precise comparator and its limitations. ' * 200
    parts = evidence.passages([{'id': 'long', 'abstract': text}])
    assert len(parts) > 1
    assert ' '.join(p['quote'] for p in parts.values()) == text.strip()
    assert all(len(p['quote']) <= 900 for p in parts.values())


@pytest.mark.parametrize('status', [403, 404, 302, 500])
def test_unavailable_full_text_preserves_abstract_and_never_follows_redirects(status):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'https://untrusted.test/private'}, content=b'not XML')
    async def check():
        src = source()
        original = copy.deepcopy(src)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as client:
            assert await evidence._open_text(client, src, 'asthma') is None
        assert src == original
    asyncio.run(check())
    assert len(requests) == 1
    assert str(requests[0].url) == 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC456/fullTextXML'


def test_open_text_rejects_oversize_body_without_consuming_more():
    class Oversize(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x' * 2_000_001
            raise AssertionError('Reader must stop at the byte cap')
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Oversize()))) as client:
            assert await evidence._open_text(client, source(), 'asthma') is None
    asyncio.run(check())
