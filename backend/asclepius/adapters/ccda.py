"""C-CDA sections to case fragments. XML entities, network and identifiers denied.

Section template identifiers: https://hl7.org/cda/us/ccda/artifacts.html
Structured fields never come from narrative text. Narratives remain subject to
the existing timeline normalization and residual-PHI quarantine gates.
"""
from __future__ import annotations

import hashlib
import math

from .ehr_mapping import PatientUnmapped, mapped_key
from asclepius.case_formats import age_to_band
from asclepius.timeline import parse_datetime

NS = {'c': 'urn:hl7-org:v3'}
SECTION_OIDS = {
    '2.16.840.1.113883.10.20.22.2.5.1': '11450-4',
    '2.16.840.1.113883.10.20.22.2.1.1': '10160-0',
    '2.16.840.1.113883.10.20.22.2.3.1': '30954-2',
    '2.16.840.1.113883.10.20.22.2.4.1': '8716-3',
    '2.16.840.1.113883.10.20.22.2.22.1': '46240-8',
    '2.16.840.1.113883.10.20.22.2.10': '18776-5',
    '2.16.840.1.113883.10.20.22.2.6.1': '48765-2',
    '2.16.840.1.113883.10.20.22.2.8': '51848-0',
    '2.16.840.1.113883.10.20.22.2.9': '51848-0',
}
SYSTEMS = {'2.16.840.1.113883.6.1': 'http://loinc.org', '2.16.840.1.113883.6.88': 'http://www.nlm.nih.gov/research/umls/rxnorm',
           '2.16.840.1.113883.6.90': 'http://hl7.org/fhir/sid/icd-10-cm'}


class CcdaParseError(ValueError):
    pass


def nodes(node, path):
    return node.xpath(path, namespaces=NS)


def attr(node, path, name='value'):
    found = nodes(node, path)
    return found[0].get(name) if found else None


def stamp(node):
    return attr(node, './c:effectiveTime') or attr(node, './c:effectiveTime/c:low')


def code_text(node):
    if node is None:
        return ''
    return node.get('displayName') or node.get('code') or ''


def numeric(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else str(value)
    except (TypeError, ValueError):
        return value


def parse(raw, *, specialty='general', manifest=None):
    from lxml import etree
    key = mapped_key(manifest)
    try:
        parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True,
                                 huge_tree=False, recover=False, remove_comments=True, remove_pis=True)
        root = etree.fromstring(raw.encode() if isinstance(raw, str) else raw, parser)
    except (etree.XMLSyntaxError, TypeError, ValueError):
        raise CcdaParseError('malformed C-CDA XML') from None
    if root.tag != '{urn:hl7-org:v3}ClinicalDocument' or root.getroottree().docinfo.doctype:
        raise CcdaParseError('expected C-CDA ClinicalDocument without DTD')
    roles = nodes(root, './c:recordTarget/c:patientRole')
    if len(roles) != 1:
        raise PatientUnmapped('C-CDA must identify exactly one patient')
    identities = nodes(roles[0], './c:id')
    if key is None:
        identity = next((i for i in identities if i.get('root') and i.get('extension')), None)
        if identity is None:
            raise PatientUnmapped('C-CDA patient identifier is missing')
        key = 'ccda-' + hashlib.sha256((identity.get('root') + '|' + identity.get('extension')).encode()).hexdigest()[:12]
    frag = {k: [] for k in ('problem_list', 'medications', 'lab_panels', 'notes', 'encounters', 'orders', 'allergies')}
    frag.update({'demographics': {}, '_patient_keys': [key], '_adapter_warnings': []})
    sex = attr(roles[0], './c:patient/c:administrativeGenderCode', 'code')
    if sex in ('M', 'F'):
        frag['demographics']['sex'] = sex
    birth = parse_datetime(attr(roles[0], './c:patient/c:birthTime'))
    dates = [parse_datetime(x) for x in nodes(root, './/c:effectiveTime/@value | .//c:effectiveTime/c:low/@value')]
    if birth and any(dates):
        at = max(d for d in dates if d)
        age = at.year - birth.year - ((at.month, at.day) < (birth.month, birth.day))
        if 0 <= age <= 120:
            frag['demographics']['age_band'] = age_to_band(age)
    # Preserve authored time, not any author identity, before dropping headers.
    authored={entry:attr(entry,'./c:author/c:time') for entry in nodes(root,'.//c:section/c:entry/*')}
    # Drop source identity-bearing nodes before touching clinical sections.
    for element in nodes(root, './/c:recordTarget | .//c:author | .//c:custodian | .//c:legalAuthenticator | .//c:addr | .//c:telecom | .//c:name'):
        if element.getparent() is not None:
            element.getparent().remove(element)
    for element in nodes(root, './/c:id'):
        element.attrib.pop('extension', None)
    recognized = 0
    for section in nodes(root, './c:component/c:structuredBody/c:component/c:section'):
        kind = next((SECTION_OIDS[x] for x in nodes(section, './c:templateId/@root') if x in SECTION_OIDS), None)
        kind = kind or attr(section, './c:code', 'code')
        if kind not in ('11450-4', '10160-0', '30954-2', '8716-3', '46240-8', '18776-5', '48765-2', '51848-0', '11488-4'):
            frag['_adapter_warnings'].append('unrecognized_ccda_section')
            continue
        recognized += 1
        if kind == '11450-4':
            for observation in nodes(section, './c:entry//c:observation'):
                values = nodes(observation, './c:value')
                if not values or observation.get('negationInd') == 'true':
                    continue
                value = values[0]
                text = code_text(value)
                if value.get('codeSystem') == '2.16.840.1.113883.6.90' and value.get('code'):
                    text = f"{text} ({value.get('code')})"
                if text:
                    frag['problem_list'].append({'condition': text, 'recorded_at': stamp(observation)})
        elif kind == '10160-0':
            for med in nodes(section, './c:entry//c:substanceAdministration'):
                codes = nodes(med, './c:consumable//c:manufacturedMaterial/c:code')
                if not codes or med.get('negationInd') == 'true':
                    continue
                drug = code_text(codes[0])
                dose = attr(med, './c:doseQuantity')
                unit = attr(med, './c:doseQuantity', 'unit') or ''
                start, stop = attr(med, './c:effectiveTime/c:low'), attr(med, './c:effectiveTime/c:high')
                status = attr(med, './c:statusCode', 'code') or 'unknown'
                freq = attr(med, './c:effectiveTime/c:period')
                freq_unit = attr(med, './c:effectiveTime/c:period', 'unit')
                item = {'drug': drug, 'dose': f'{dose} {unit}'.strip() if dose else None,
                        'route': attr(med, './c:routeCode', 'displayName'),
                        'freq': f'every {freq} {freq_unit}' if freq and freq_unit else None,
                        'status': status if status in ('active', 'completed', 'stopped') else 'unknown',
                        'started_at': start, 'stopped_at': stop, 'collected_at': start or stamp(med)}
                frag['medications'].append(item)
        elif kind in ('30954-2', '8716-3'):
            grouped = {}
            for obs in nodes(section, './c:entry//c:observation'):
                codes, values = nodes(obs, './c:code'), nodes(obs, './c:value')
                if not codes or not values or obs.get('negationInd') == 'true' or values[0].get('nullFlavor'):
                    continue
                code, value = codes[0], values[0]
                raw_value = value.get('value') or value.get('displayName')
                if raw_value is None:
                    continue
                at = stamp(obs)
                if at is None:
                    ancestors = nodes(obs, 'ancestor::c:organizer[1]')
                    at = stamp(ancestors[0]) if ancestors else None
                result = {'analyte': code_text(code), 'loinc': code.get('code') if code.get('codeSystem') in (None, '2.16.840.1.113883.6.1') else None,
                          'value': numeric(raw_value), 'unit': value.get('unit'),
                          'ref_low': numeric(attr(obs, './c:referenceRange//c:value/c:low')),
                          'ref_high': numeric(attr(obs, './c:referenceRange//c:value/c:high')),
                          'flag': attr(obs, './c:interpretationCode', 'code') or ''}
                if result['flag'] not in ('L', 'H', 'LL', 'HH'):
                    result['flag'] = ''
                grouped.setdefault(at, []).append(result)
            for at, results in grouped.items():
                if at is None:
                    # LabPanel defaults unknown timing to day zero; do not let a
                    # timestamp-free C-CDA observation acquire invented timing.
                    raise CcdaParseError('C-CDA result is missing collection time')
                frag['lab_panels'].append({'panel': 'Vitals' if kind == '8716-3' else 'Labs',
                                           'collected_at': at, 'results': results})
        elif kind == '46240-8':
            for encounter in nodes(section, './c:entry//c:encounter'):
                frag['encounters'].append({'encounter_ref': 'pending', 'visit_type': 'office', 'collected_at': stamp(encounter)})
        elif kind == '18776-5':
            for entry in nodes(section, './c:entry/*'):
                tag = etree.QName(entry).localname
                codes = nodes(entry, './c:code')
                code = codes[0] if codes else None
                frag['orders'].append({'kind': {'observation':'lab', 'encounter':'follow_up'}.get(tag, 'other'),
                                       'text': code_text(code), 'code': code.get('code') if code is not None else None,
                                       'code_system': SYSTEMS.get(code.get('codeSystem')) if code is not None else None,
                                       'due_at': stamp(entry), 'collected_at':authored.get(entry)})
        elif kind == '48765-2':
            for obs in nodes(section, './c:entry//c:observation[c:participant]'):
                if obs.get('negationInd') == 'true':
                    continue
                substance = nodes(obs, './c:participant//c:playingEntity/c:code')
                reaction = nodes(obs, './c:entryRelationship/c:observation/c:value')
                if substance:
                    frag['allergies'].append({'substance': code_text(substance[0]),
                                             'reaction': code_text(reaction[0]) if reaction else None, 'recorded_at': stamp(obs)})
        else:
            texts = nodes(section, './c:text')
            text = '\n'.join(''.join(node.itertext()) for node in texts).strip()
            if text:
                at = attr(section, './c:entry/*/c:effectiveTime') or stamp(section)
                if at is None:
                    from .pdf_doc import service_date
                    at = service_date(text)
                title = nodes(section, './c:title/text()')
                frag['notes'].append({'note_type': 'Visit worksheet' if kind == '51848-0' else (title[0] if title else 'Consult'),
                                      'text': text, 'author_role': 'clinician', 'collected_at': at})
    if not recognized or not any(frag[k] for k in ('notes', 'lab_panels', 'medications', 'problem_list', 'orders', 'allergies', 'encounters')):
        frag['_unparsed_reason'] = 'no recognizable clinical content in C-CDA'
    frag['encounters'].sort(key=lambda x: x.get('collected_at') or '')
    for i, encounter in enumerate(frag['encounters']):
        encounter['encounter_ref'] = f'enc-{i:03d}'
    return frag
