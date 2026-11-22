import base64
from pathlib import Path
import tempfile
import unittest

from cryptography.fernet import Fernet, InvalidToken
from vault import Vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = Fernet.generate_key()
        self.vault = Vault(self.root / 'persistent', self.key)

    def test_session_round_trip_never_persists_plaintext(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'cookies.jar').write_bytes(b'synthetic-twitch-oauth-secret')
        (runtime / 'settings.json').write_text('{"games_to_watch":["Example"]}')
        self.vault.snapshot('account-one', runtime)
        persisted = list((self.root / 'persistent').iterdir())
        self.assertEqual([p.name for p in persisted], ['account-one.enc'])
        self.assertNotIn(b'synthetic-twitch', persisted[0].read_bytes())
        self.assertNotIn(b'games_to_watch', persisted[0].read_bytes())
        restored = self.root / 'restored'
        Vault(self.root / 'persistent', self.key).restore('account-one', restored)
        self.assertEqual((runtime / 'cookies.jar').read_bytes(), (restored / 'cookies.jar').read_bytes())
        self.assertEqual((restored / 'cookies.jar').stat().st_mode & 0o777, 0o600)

    def test_wrong_key_and_tampering_fail_closed(self):
        self.vault.save('registry', ['private-label'])
        with self.assertRaises(InvalidToken):
            Vault(self.root / 'persistent', Fernet.generate_key()).load('registry')
        file = self.root / 'persistent' / 'registry.enc'
        token = bytearray(file.read_bytes())
        token[len(token) // 2] ^= 1
        file.write_bytes(token)
        with self.assertRaises(InvalidToken):
            self.vault.load('registry')

    def test_swapped_account_records_are_rejected(self):
        self.vault.save('one', {'token': 'one'})
        (self.root / 'persistent' / 'two.enc').write_bytes((self.root / 'persistent' / 'one.enc').read_bytes())
        with self.assertRaises(ValueError):
            self.vault.load('two')

    def test_restore_rejects_traversal(self):
        self.vault.save('one', {'../escape': base64.b64encode(b'bad').decode()})
        with self.assertRaises(ValueError):
            self.vault.restore('one', self.root / 'restored')
        self.assertFalse((self.root / 'escape').exists())

    def test_symlinks_are_not_checkpointed(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (self.root / 'external').write_text('must-not-copy')
        (runtime / 'link').symlink_to(self.root / 'external')
        self.vault.snapshot('one', runtime)
        self.assertEqual(self.vault.load('one'), {})
