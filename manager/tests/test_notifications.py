import asyncio
import copy
import json
import os
from pathlib import Path
import ssl
import struct
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from mqtt_notifications import EVENTS, MqttPublisher, default_config, validate_config
from notifications import NotificationHub
from vault import Vault

sys.path.insert(0, '/app')
os.environ.setdefault('TDM_DATA_DIR', '/tmp/notification-test-data')
from src.services.manager_notifications import ManagerNotifications
from src.models.benefit import BenefitType


def observation(linked=True, **changes):
    return {'logged_in': True, 'renewal_error': None, 'inventory_ready': True,
            'observed_at': time.time(), 'refresh_minutes': 30,
            'channels': {'telegram': True, 'discord': True},
            'campaigns': [{'id': 'campaign-a', 'game': 'Game A', 'campaign': 'Campaign A',
                           'linked': linked, 'link_url': 'https://example.com/link'}],
            'claims': [], **changes}


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Vault(Path(self.temp.name), Fernet.generate_key())
        self.account = {'id': 'one', 'name': 'Account One', 'enabled': True, 'port': 12000}
        self.manager = SimpleNamespace(vault=self.vault, accounts=[self.account])
        self.manager.account = lambda key: next((a for a in self.manager.accounts if a['id'] == key), None)
        self.hub = NotificationHub(self.manager)

    async def asyncTearDown(self):
        await self.hub.close()
        self.temp.cleanup()

    async def test_disabled_default_needs_no_broker_and_password_is_redacted(self):
        config = default_config()
        config['mqtt'].update(host='localhost', username='synthetic-user', password='synthetic-broker-secret')
        await self.hub.configure(config)
        public = self.hub.public()
        self.assertEqual(public['mqtt']['connection'], 'disabled')
        self.assertNotIn('password', public['settings']['mqtt'])
        self.assertTrue(public['settings']['mqtt']['password_set'])
        self.assertNotIn('synthetic-broker-secret', json.dumps(public))
        self.assertNotIn(b'synthetic-broker-secret', (Path(self.temp.name) / 'notification-settings.enc').read_bytes())
        config['mqtt']['password'] = ''
        await self.hub.configure(config)
        self.assertEqual(self.hub.config['mqtt']['password'], 'synthetic-broker-secret')
        config['mqtt']['clear_password'] = True
        await self.hub.configure(config)
        self.assertFalse(self.hub.public()['settings']['mqtt']['password_set'])

    async def test_link_loss_restoration_first_unlinked_and_restart_deduplication(self):
        self.hub.observe(self.account, observation())
        self.assertEqual(len(self.hub.recent), 0)
        self.hub.observe(self.account, observation(False))
        self.assertEqual(self.hub.recent[-1]['event'], 'account_link_required')
        self.assertEqual(self.hub.recent[-1]['change'], 'lost')
        self.assertEqual(self.hub.queue.qsize(), 1)
        self.hub.observe(self.account, observation(False))
        self.assertEqual(self.hub.queue.qsize(), 1)
        restarted = NotificationHub(self.manager)
        restarted.observe(self.account, observation(False))
        self.assertEqual(restarted.queue.qsize(), 0)
        self.hub.observe(self.account, observation())
        self.assertEqual(self.hub.recent[-1]['event'], 'account_link_restored')
        self.hub.saved['accounts'].clear()
        self.hub.observe(self.account, observation(False))
        self.assertEqual(self.hub.recent[-1]['change'], 'unlinked')

    async def test_unavailable_fetching_and_stale_data_never_become_link_loss(self):
        self.hub.observe(self.account, observation())
        self.hub.observe(self.account, observation(False, inventory_ready=False))
        self.hub.observe(self.account, observation(False, observed_at=time.time() - 100000))
        self.assertFalse(self.hub.recent)
        with self.assertRaises(ValueError):
            self.hub.observe(self.account, {'logged_in': 'unknown'})
        self.assertTrue(self.hub.saved['accounts']['one']['links']['campaign-a'])

    async def test_claim_history_baseline_only_new_claims_emit_and_no_native_duplicates(self):
        old = {'id': 'old', 'game': 'A', 'campaign': 'B', 'reward': 'C'}
        new = {**old, 'id': 'new'}
        self.hub.observe(self.account, observation(claims=[old]))
        self.hub.observe(self.account, observation(claims=[new, old]))
        self.assertEqual([e['event'] for e in self.hub.recent], ['drop_claimed'])
        self.assertTrue(self.hub.queue.empty())
        self.hub.observe(self.account, observation(claims=[new]))
        self.assertEqual(len(self.hub.recent), 1)

    async def test_account_and_event_filters_and_storage_failure(self):
        config = default_config()
        config['accounts']['one'] = {'telegram': False, 'discord': False, 'mqtt': False, 'events': []}
        await self.hub.configure(config)
        self.hub.observe(self.account, observation(False))
        self.assertTrue(self.hub.queue.empty())
        self.assertFalse(self.hub.recent)
        self.assertIsNone(self.hub.publisher.states['one'])
        self.hub.config = default_config()
        self.hub.saved['accounts'].clear()
        with patch.object(self.vault, 'save', side_effect=OSError):
            self.hub.observe(self.account, observation(False))
        self.assertNotIn('one', self.hub.saved['accounts'])
        self.assertTrue(self.hub.queue.empty())

    async def test_login_renewal_errors_are_transitions_and_account_deletion_cleans(self):
        self.hub.observe(self.account, observation())
        self.hub.observe(self.account, observation(logged_in=False, renewal_error='AUTH'))
        self.hub.observe(self.account, observation(logged_in=False, renewal_error='AUTH'))
        self.assertEqual([e['event'] for e in self.hub.recent], ['login_required', 'renewal_failed'])
        self.assertEqual(len(NotificationHub(self.manager).recent), 2)
        self.manager.accounts = []
        self.assertFalse(NotificationHub(self.manager).recent)
        self.manager.accounts = [self.account]
        self.hub.publisher.config['enabled'] = True
        self.hub.publisher.event('one', {'event': 'login_required'})
        await self.hub.forget('one')
        self.assertFalse(self.hub.publisher.events)
        self.assertNotIn('one', self.hub.saved['accounts'])
        self.assertNotIn('one', self.hub.observations)
        self.assertIsNone(self.hub.publisher.states['one'])
        self.assertFalse(self.hub.recent)

    async def test_failed_native_delivery_is_reported_without_provider_details(self):
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def json(self): return {'telegram': False, 'discord': True}
        self.manager.client = SimpleNamespace(post=lambda *a, **kw: Response())
        self.manager.public_origin = None
        self.hub.observe(self.account, observation(False))
        task = asyncio.create_task(self.hub.deliver())
        try:
            for _ in range(20):
                if self.hub.error:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(self.hub.error, 'Account notification delivery failed')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_settings_and_credential_urls_rejected(self):
        for change in ({'host': 'mqtt://localhost'}, {'topic_prefix': 'bad/#'}, {'port': True},
                       {'enabled': True, 'host': ''}, {'tls': 'yes'}):
            config = default_config()
            config['mqtt'].update(change)
            with self.assertRaises(ValueError):
                validate_config(config, default_config(), ['one'])
        data = observation(False)
        data['campaigns'][0]['link_url'] = 'https://example.com/?token=never-publish'
        self.hub.observe(self.account, data)
        self.assertNotIn('never-publish', json.dumps(self.hub.public()))


class WorkerBridgeTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        login = asyncio.Event()
        login.set()
        settings = SimpleNamespace(games_to_watch=['Game A'], mining_benefits={'DIRECT_ENTITLEMENT': True},
            minimum_refresh_interval_minutes=30, telegram_bot_token='synthetic-bot-secret',
            telegram_chat_id='1234', discord_webhook_url='')
        from datetime import datetime, timedelta, timezone
        drop = SimpleNamespace(benefits=[SimpleNamespace(type=BenefitType.DIRECT_ENTITLEMENT)],
                               ends_at=datetime.now(timezone.utc) + timedelta(days=1),
                               has_wanted_unclaimed_benefits=lambda allowed: True)
        from datetime import datetime, timedelta, timezone
        campaign = SimpleNamespace(id='a', game=SimpleNamespace(name='Game A'), name='Campaign A',
            linked=False, link_url='https://example.com/link', ends_at=datetime.now(timezone.utc) + timedelta(days=1), watch_drops=[drop])
        return SimpleNamespace(_auth_state=SimpleNamespace(_logged_in=login), settings=settings,
            session_controller=SimpleNamespace(status=lambda: {'renewal_error': None}),
            drop_history=SimpleNamespace(get_entries=lambda limit: []), inventory=[campaign],
            manager_inventory_at=time.time(), manager_inventory_fetching=False)

    async def test_snapshot_selects_wanted_campaigns_without_exposing_credentials(self):
        self.assertIsNone(ManagerNotifications.public_link('https://example.com:wrong/link'))
        client = self.client()
        data = ManagerNotifications.snapshot(client)
        self.assertEqual(len(data['campaigns']), 1)
        self.assertFalse(data['campaigns'][0]['linked'])
        self.assertNotIn('synthetic-bot-secret', json.dumps(data))
        client.manager_inventory_fetching = True
        self.assertFalse(ManagerNotifications.snapshot(client)['inventory_ready'])
        self.assertFalse(ManagerNotifications.snapshot(client)['campaigns'])
        client.manager_inventory_fetching = False
        client.settings.games_to_watch = ['Other Game']
        self.assertFalse(ManagerNotifications.snapshot(client)['campaigns'])
        client.settings.games_to_watch = ['Game A']
        from datetime import datetime, timedelta, timezone
        client.inventory[0].watch_drops[0].ends_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertFalse(ManagerNotifications.snapshot(client)['campaigns'])

    async def test_native_delivery_uses_existing_destinations_and_escapes_telegram(self):
        with patch('src.services.manager_notifications.TelegramNotifier') as telegram, patch('src.services.manager_notifications.DiscordNotifier') as discord:
            telegram.return_value._send_message = AsyncMock(return_value=True)
            discord.return_value._send_embed = AsyncMock(return_value=True)
            result = await ManagerNotifications.send(self.client(), {'account': '<account>', 'telegram': True,
                'discord': False, 'events': [{'event': 'account_link_required', 'game': '<Game>', 'campaign': 'A'}]})
            self.assertTrue(result['telegram'])
            self.assertFalse(result['discord'])
            text = telegram.return_value._send_message.call_args.args[0]
            self.assertIn('&lt;Game&gt;', text)
            self.assertNotIn('<account>', text)
            discord.return_value._send_embed.assert_not_awaited()


class Broker:
    """Minimal local MQTT 3.1.1 test peer; never contacts an external broker."""
    def __init__(self, drop_event_ack=False):
        self.messages = []
        self.writers = set()
        self.drop_event_ack = drop_event_ack
        self.connections = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()

    async def handle(self, reader, writer):
        self.writers.add(writer)
        try:
            while True:
                header = (await reader.readexactly(1))[0]
                size, multiplier = 0, 1
                while True:
                    digit = (await reader.readexactly(1))[0]
                    size += (digit & 127) * multiplier
                    multiplier *= 128
                    if not digit & 128:
                        break
                body = await reader.readexactly(size)
                kind = header >> 4
                if kind == 1:
                    self.connections += 1
                    writer.write(b'\x20\x02\x00\x00')
                elif kind == 3:
                    length = struct.unpack('!H', body[:2])[0]
                    topic = body[2:2 + length].decode()
                    offset = 2 + length
                    qos = (header >> 1) & 3
                    ident = body[offset:offset + 2] if qos else b''
                    payload = body[offset + (2 if qos else 0):].decode()
                    self.messages.append((topic, payload, bool(header & 1), qos))
                    if topic.endswith('/events') and self.drop_event_ack:
                        self.drop_event_ack = False
                        writer.close()
                        break
                    if qos == 1:
                        writer.write(b'\x40\x02' + ident)
                elif kind == 12:
                    writer.write(b'\xd0\x00')
                elif kind == 14:
                    break
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            self.writers.discard(writer)


class MqttTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate):
        for _ in range(300):
            if predicate():
                return
            await asyncio.sleep(.02)
        self.fail('MQTT condition timed out')

    async def test_real_client_retained_state_event_qos_and_offline_availability(self):
        broker = Broker()
        await broker.start()
        publisher = MqttPublisher('test-instance')
        config = default_config()['mqtt']
        config.update(enabled=True, host='127.0.0.1', port=broker.port)
        publisher.state('one', {'status': 'signed_in'})
        await publisher.configure(config)
        try:
            publisher.event('one', {'event': 'account_link_required', 'event_id': 'synthetic-id'})
            await self.wait_for(lambda: publisher.status['last_published_at'])
            event = next(m for m in broker.messages if m[0].endswith('/events'))
            self.assertFalse(event[2])
            self.assertEqual(event[3], 1)
            self.assertTrue(next(m for m in broker.messages if m[0].endswith('/state'))[2])
            publisher.state('one', None)
            await self.wait_for(lambda: any(m[0].endswith('/state') and m[1] == '' for m in broker.messages))
            await publisher.close()
            self.assertTrue(any(m[0].endswith('/availability') and m[1] == 'offline' and m[2] for m in broker.messages))
        finally:
            await publisher.close()
            await broker.close()

    async def test_disconnect_retries_same_event_id_and_queue_is_bounded(self):
        broker = Broker(drop_event_ack=True)
        await broker.start()
        publisher = MqttPublisher('test-instance')
        config = default_config()['mqtt']
        config.update(enabled=True, host='127.0.0.1', port=broker.port)
        await publisher.configure(config)
        try:
            publisher.event('one', {'event': 'login_required', 'event_id': 'same-id'})
            await self.wait_for(lambda: len([m for m in broker.messages if m[0].endswith('/events')]) >= 2)
            events = [json.loads(m[1]) for m in broker.messages if m[0].endswith('/events')]
            self.assertTrue(all(e['event_id'] == 'same-id' for e in events))
            self.assertGreaterEqual(broker.connections, 2)
        finally:
            await publisher.close()
            await broker.close()
        for index in range(300):
            publisher.event('one', {'event_id': str(index)})
        self.assertEqual(len(publisher.events), 256)
        self.assertEqual(publisher.status['dropped_events'], 44)

    async def test_tls_uses_certificate_and_hostname_validation(self):
        captured = {}
        class Client:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            async def __aenter__(self):
                raise ConnectionError
            async def __aexit__(self, *args):
                pass
        publisher = MqttPublisher('tls-test', factory=Client)
        config = default_config()['mqtt']
        config.update(enabled=True, host='localhost', tls=True)
        await publisher.configure(config)
        try:
            await self.wait_for(lambda: bool(captured))
            context = captured['tls_context']
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        finally:
            await publisher.close()
