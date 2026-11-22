"""Authenticated encrypted persistence. Plaintext lives only in runtime tmpfs."""
import base64
import json
import os
from pathlib import Path, PurePosixPath

from cryptography.fernet import Fernet


class Vault:
    def __init__(self, root: Path, key: bytes):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.cipher = Fernet(key.strip())

    def save(self, scope: str, payload):
        token = self.cipher.encrypt(json.dumps({'scope': scope, 'payload': payload}).encode())
        path = self.root / f'{scope}.enc'
        temp = path.with_suffix('.tmp')
        with open(temp, 'wb') as stream:
            os.chmod(temp, 0o600)
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def load(self, scope: str, default=None):
        path = self.root / f'{scope}.enc'
        if not path.exists():
            return default
        envelope = json.loads(self.cipher.decrypt(path.read_bytes()))
        if envelope['scope'] != scope:
            raise ValueError('Encrypted record belongs to another account')
        return envelope['payload']

    def delete(self, scope: str):
        for suffix in ('.enc', '.tmp'):
            (self.root / (scope + suffix)).unlink(missing_ok=True)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def snapshot(self, account: str, data: Path):
        files = {}
        for file in data.rglob('*'):
            if file.is_file() and not file.is_symlink():
                try:
                    files[str(file.relative_to(data))] = base64.b64encode(file.read_bytes()).decode()
                except FileNotFoundError:
                    pass
        self.save(account, files)

    def restore(self, account: str, data: Path):
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, encoded in self.load(account, {}).items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or '..' in relative.parts or '\\' in name:
                raise ValueError('Invalid encrypted file path')
            target = data / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(base64.b64decode(encoded, validate=True))
            target.chmod(0o600)
