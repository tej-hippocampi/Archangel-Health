"""Pure serialization and FHIR helpers shared by the runtime and control plane."""
from __future__ import annotations
import base64
import hashlib
import importlib
import json

from .constants import OFFSET_URL


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def identifier(prefix, value):
    return prefix + '-' + digest(value)[:12]


def extension(name, value, kind='Integer'):
    return {'url': OFFSET_URL.replace('offsetDays', name), 'value' + kind: value}


def ext(resource, name='offsetDays', default=None):
    url = OFFSET_URL.replace('offsetDays', name)
    for item in resource.get('extension', []):
        if item.get('url') == url:
            return next((v for k,v in item.items() if k.startswith('value')), default)
    return default


def concept(text, code=None, system=None):
    value = {'text': str(text)}
    if code and system:
        value['coding'] = [{'system': system, 'code': str(code), 'display': str(text)}]
    return value


def resource_ref(resource):
    return resource['resourceType'] + '/' + resource['id']


def subject(resource):
    if resource.get('resourceType') == 'Patient':
        return resource['id']
    ref = (resource.get('subject') or resource.get('patient') or {}).get('reference', '')
    if resource.get('resourceType') == 'Appointment':
        ref = next((p.get('actor', {}).get('reference','') for p in resource.get('participant', [])
                    if p.get('actor', {}).get('reference', '').startswith('Patient/')), '')
    return ref.removeprefix('Patient/')


def document_text(resource):
    texts = []
    for item in resource.get('content', []):
        data = item.get('attachment', {}).get('data')
        if data:
            texts.append(base64.b64decode(data, validate=True).decode('utf-8'))
    return '\n'.join(texts)


def validate(resource):
    name = resource.get('resourceType', '')
    if name not in {'Patient','Encounter','Observation','DiagnosticReport','MedicationRequest','Condition',
                    'DocumentReference','ServiceRequest','Appointment','AllergyIntolerance','Flag','CommunicationRequest','Bundle'}:
        raise ValueError('unsupported resource type')
    cls = getattr(importlib.import_module('fhir.resources.R4B.' + name.lower()), name)
    cls.model_validate(resource)
    return resource


def outcome(message, code='invalid'):
    return {'resourceType': 'OperationOutcome', 'issue': [{'severity': 'error', 'code': code, 'diagnostics': message}]}
