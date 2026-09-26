"""Control-plane-only key encryption. Never import this module in the agent server."""
import json
from field_crypto import decrypt_field, encrypt_field, is_configured, is_encrypted
from .common import dumps


def seal(value):
    if not is_configured():
        raise ValueError('EHR sealing requires a configured DATA_ENCRYPTION_KEY')
    token = encrypt_field(dumps(value))
    if not is_encrypted(token):
        raise ValueError('EHR answer key was not encrypted')
    return token


def unseal(token):
    if not is_encrypted(token):
        raise ValueError('EHR answer key must be encrypted')
    return json.loads(decrypt_field(token))
