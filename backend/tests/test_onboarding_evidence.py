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


@pytest.mark.parametrize('title,mesh,scope,expected', [
    ('Sepsis in Dogs and Cats', set(), 'adult', False),
    ('Experimental treatment guidance', {'Animals'}, 'adult', False),
    ('Human and animal treatment evidence', {'Animals', 'Humans'}, 'adult', True),
    ('Unindexed clinical guidance', set(), 'adult', True),
    ('Sepsis in pediatric patients', {'Humans'}, 'adult', False),
    ('Adult and pediatric sepsis', {'Humans'}, 'adult', True),
    ('Guidance for infants and children', set(), 'pediatric', True),
    ('Guidance for older adults', {'Humans'}, 'pediatric', False),
    ('Anticoagulation in Child-Pugh A cirrhosis', {'Humans'}, 'adult', True),
    ('Childhood-onset disease outcomes', {'Humans', 'Child', 'Adult'}, 'adult', True),
    ('Maternal and neonatal outcomes after cesarean delivery', {'Humans', 'Adult', 'Infant, Newborn'}, 'adult', True),
    ('Guideline endorsed by the Society of Pediatric Nutrition', {'Humans'}, 'adult', True),
    ('Treatment guidance', {'Humans', 'Child'}, 'adult', False),
    ('Treatment guidance', {'Humans', 'Adult', 'Child'}, 'pediatric', True),
])
def test_retrieval_excludes_only_explicit_population_mismatches(title, mesh, scope, expected):
    assert evidence.population_matches(title, mesh, scope) is expected


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


def test_plain_text_license_is_read_only_from_permissions():
    raw = article().replace(b'<ext-link xlink:href="https://creativecommons.org/licenses/by/4.0/">Terms</ext-link>',
                            b'This is open access (http://creativecommons.org/licenses/by/4.0/).')
    assert evidence.parse_open_text(raw, source(), 'asthma')['body_license_url'] == 'http://creativecommons.org/licenses/by/4.0/'
    assert evidence.parse_open_text(raw.replace(b'licenses/by/', b'licenses/by-nc-nd/'), source(), 'asthma') is None
    assert evidence.parse_open_text(raw.replace(b'<permissions>', b'<other>').replace(b'</permissions>', b'</other>'), source(), 'asthma') is None


def test_pathology_teaching_exception_requires_exact_identity_and_no_retraction():
    from datetime import datetime, timezone
    raw = b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>40687210</PMID><Article>
        <Journal><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>Educational Case: Squamous cell carcinoma.</ArticleTitle>
        <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
        </Article></MedlineCitation><PubmedData><ArticleIdList>
        <ArticleId IdType="pmc">PMC12271062</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'''
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert evidence.parse_articles(raw, now=now) == []
    rows = evidence.parse_articles(raw, now=now, pathology_teaching=True)
    assert rows[0]['source_type'] == 'peer_reviewed_pathology_teaching'
    assert rows[0]['abstract'] == ''  # no invented abstract for a full-text-only teaching paper
    for damaged in (raw.replace(b'40687210', b'99999999'), raw.replace(b'PMC12271062', b'PMC123'),
                    raw.replace(b'carcinoma.', b'other.'), raw.replace(b'Journal Article', b'Retracted Publication')):
        assert evidence.parse_articles(damaged, now=now, pathology_teaching=True) == []
    unrelated_guideline = raw.replace(b'40687210', b'99999999').replace(b'Journal Article', b'Guideline').replace(
        b'</Article>', b'<Abstract><AbstractText>' + b'Fictional guideline abstract. ' * 20 + b'</AbstractText></Abstract></Article>')
    assert len(evidence.parse_articles(unrelated_guideline, now=now)) == 1
    assert evidence.parse_articles(unrelated_guideline, now=now, pathology_teaching=True) == []


def test_misindexed_guidance_pin_requires_exact_identity_and_preserves_safety_filters():
    from datetime import datetime, timezone
    raw = b'''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>38152089</PMID><Article>
        <Journal><JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>Guideline for the management of myasthenic syndromes.</ArticleTitle>
        <Abstract><AbstractText>''' + b'Fictional source for parser testing. ' * 20 + b'''</AbstractText></Abstract>
        <PublicationTypeList><PublicationType>Review</PublicationType></PublicationTypeList>
        </Article></MedlineCitation><PubmedData><ArticleIdList>
        <ArticleId IdType="pmc">PMC10752078</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>'''
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    pins = {'38152089': {'pmcid': 'PMC10752078', 'title': 'Guideline for the management of myasthenic syndromes.',
                         'source_type': 'primary_society_guideline'}}
    assert evidence.parse_articles(raw, now=now) == []
    assert evidence.parse_articles(raw, now=now, pinned=pins)[0]['source_type'] == 'primary_society_guideline'
    for old, new in [(b'38152089', b'11111111'), (b'PMC10752078', b'PMC123'),
                     (b'syndromes.', b'other.'), (b'>2023<', b'>2010<'),
                     (b'>Review<', b'>Retracted Publication<')]:
        assert evidence.parse_articles(raw.replace(old, new), now=now, pinned=pins) == []


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


@pytest.mark.parametrize('recovers', [True, False])
def test_reference_throttle_retries_are_bounded_and_keep_exact_response(monkeypatch, recovers):
    requests, waits = [], []
    async def sleep(delay):
        waits.append(delay)
    monkeypatch.setattr(evidence.asyncio, 'sleep', sleep)
    def respond(request):
        requests.append(request)
        if recovers and len(requests) == 2:
            return httpx.Response(200, content=b'exact clinical reference')
        return httpx.Response(429, headers={'Retry-After': '3'})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            if recovers:
                assert await evidence._request(client, 'efetch.fcgi', {'id': '123'}) == b'exact clinical reference'
            else:
                with pytest.raises(httpx.HTTPStatusError):
                    await evidence._request(client, 'efetch.fcgi', {'id': '123'})
    asyncio.run(check())
    assert len(requests) == (2 if recovers else 3)
    assert waits.count(3) == (1 if recovers else 2)


def test_type_a_query_omits_unindexed_stopword_but_keeps_disease(monkeypatch):
    queries = []
    async def request(client, endpoint, params):
        queries.append(params['term'])
        return b'{"esearchresult":{"idlist":[]}}'
    monkeypatch.setattr(evidence, '_request', request)
    from asclepius.onboarding_catalog import topic_for
    with pytest.raises(ValueError, match='Insufficient clinical reference'):
        asyncio.run(evidence.retrieve('cardiothoracic surgery', topic=topic_for('cardiothoracic surgery', 'practice')))
    assert len(queries) == 4
    assert 'guideline[Publication Type]' in queries[0]
    assert 'systematic review[Publication Type]' not in queries[0]
    assert 'systematic review[Publication Type]' in queries[2]
    assert all('A[Title' not in q and 'dissection*[Title' in q and 'aortic*[Title' in q for q in queries)


def test_tagged_reference_search_preserves_plural_guideline_titles(monkeypatch):
    queries = []
    async def request(client, endpoint, params):
        queries.append(params['term'])
        return b'{"esearchresult":{"idlist":[]}}'
    monkeypatch.setattr(evidence, '_request', request)
    from asclepius.onboarding_catalog import topic_for
    with pytest.raises(ValueError, match='Insufficient clinical reference'):
        asyncio.run(evidence.retrieve('pediatrics', topic=topic_for('pediatrics', 'examination')))
    assert 'febrile*[Title] AND infant*[Title]' in queries[0]
