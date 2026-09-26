"""Explicit, per-file grouping for mixed patient archives; never guess identity."""


class PatientUnmapped(ValueError):
    pass


def mapped_key(manifest):
    manifest = manifest or {}
    mapping = manifest.get('files')
    if mapping is None:
        return None
    if not isinstance(mapping, dict) or manifest.get('patient_key'):
        raise PatientUnmapped('per-file mapping cannot coexist with a global patient_key')
    label = mapping.get(manifest.get('filename'))
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        raise PatientUnmapped('file has no valid patient mapping')
    return 'manifest-' + label.strip()
