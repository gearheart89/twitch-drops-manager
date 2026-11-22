"""Dashboard password hashing; the storage encryption key is independent."""
import hashlib
import hmac
import secrets


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
    return {'version': 1, 'salt': salt.hex(), 'hash': digest.hex()}


def verify_password(password, record):
    if not isinstance(record, dict) or set(record) != {'version', 'salt', 'hash'} or record.get('version') != 1:
        raise ValueError('Unsupported dashboard password record')
    salt = bytes.fromhex(record['salt'])
    expected = bytes.fromhex(record['hash'])
    if len(salt) != 16 or len(expected) != 64:
        raise ValueError('Invalid dashboard password record')
    actual = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
    return hmac.compare_digest(expected, actual)
