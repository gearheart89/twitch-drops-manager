"""Optional, outbound-only MQTT publishing, independent of mining workers."""
import asyncio
import json
import logging
import re
import ssl
import time
from collections import deque


EVENTS = ('account_link_required', 'account_link_restored', 'drop_claimed',
          'login_required', 'renewal_failed')


def default_config():
    return {'mqtt': {'enabled': False, 'host': '', 'port': 1883, 'tls': False,
                     'username': '', 'password': '', 'topic_prefix': 'twitch-drops'},
            'events': dict.fromkeys(EVENTS, True), 'accounts': {}}


def validate_config(value, previous, account_ids):
    if not isinstance(value, dict) or set(value) != {'mqtt', 'events', 'accounts'}:
        raise ValueError('Invalid notification settings')
    mqtt = value['mqtt']
    fields = set(default_config()['mqtt'])
    if not isinstance(mqtt, dict) or set(mqtt) - (fields | {'clear_password'}) or fields - {'password'} - set(mqtt):
        raise ValueError('Invalid MQTT settings')
    mqtt = dict(mqtt)
    clear = mqtt.pop('clear_password', False)
    if type(clear) is not bool or any(type(mqtt[k]) is not bool for k in ('enabled', 'tls')):
        raise ValueError('Invalid MQTT switch')
    if type(mqtt['port']) is not int or not 1 <= mqtt['port'] <= 65535:
        raise ValueError('MQTT port must be between 1 and 65535')
    if not isinstance(mqtt['host'], str) or (mqtt['host'] and not re.fullmatch(r'[A-Za-z0-9_.:\-]{1,253}', mqtt['host'])):
        raise ValueError('Enter a broker hostname or IP address, without a URL scheme')
    if mqtt['enabled'] and not mqtt['host']:
        raise ValueError('A broker host is required when MQTT is enabled')
    if not isinstance(mqtt['topic_prefix'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_\-]{0,127}', mqtt['topic_prefix']):
        raise ValueError('Use a topic prefix without wildcards or spaces')
    mqtt['topic_prefix'] = mqtt['topic_prefix'].rstrip('/')
    for field in ('username', 'password'):
        if not isinstance(mqtt.get(field, ''), str) or len(mqtt.get(field, '')) > 1024 or '\x00' in mqtt.get(field, ''):
            raise ValueError('Invalid MQTT credential')
    mqtt['password'] = '' if clear else mqtt.get('password') or previous['mqtt']['password']
    events = value['events']
    if not isinstance(events, dict) or set(events) != set(EVENTS) or any(type(v) is not bool for v in events.values()):
        raise ValueError('Invalid event selections')
    rules = value['accounts']
    if not isinstance(rules, dict) or set(rules) - set(account_ids):
        raise ValueError('Unknown account in notification settings')
    for rule in rules.values():
        if (not isinstance(rule, dict) or set(rule) != {'telegram', 'discord', 'mqtt', 'events'}
                or any(type(rule[k]) is not bool for k in ('telegram', 'discord', 'mqtt'))
                or not isinstance(rule['events'], list) or any(not isinstance(e, str) for e in rule['events'])
                or len(rule['events']) != len(set(rule['events']))
                or any(event not in EVENTS for event in rule['events'])):
            raise ValueError('Invalid account notification selections')
    return {'mqtt': mqtt, 'events': dict(events), 'accounts': rules}


class MqttPublisher:
    MAX_EVENTS = 256
    EVENT_TTL = 3600

    def __init__(self, instance_id, factory=None):
        self.instance_id = instance_id
        self.factory = factory
        self.config = default_config()['mqtt']
        self.states = {}
        self.events = deque()
        self.wake = asyncio.Event()
        self.task = None
        self.status = {'connection': 'disabled', 'last_published_at': None, 'dropped_events': 0}

    @property
    def root(self):
        return self.config['topic_prefix'] + '/' + self.instance_id

    async def configure(self, config):
        await self.close()
        self.events.clear()  # Never deliver queued messages to a newly chosen broker.
        self.config = dict(config)
        self.status['last_published_at'] = None
        self.status['connection'] = 'connecting' if config['enabled'] else 'disabled'
        if config['enabled']:
            self.task = asyncio.create_task(self.run())

    def state(self, account_id, value):
        if self.states.get(account_id, object()) != value:
            self.states[account_id] = value
            self.wake.set()

    def event(self, account_id, value):
        if not self.config['enabled']:
            return False
        if len(self.events) >= self.MAX_EVENTS:
            self.events.popleft()
            self.status['dropped_events'] += 1
        self.events.append((time.monotonic(), account_id, value))
        self.wake.set()
        return True

    async def run(self):
        import aiomqtt
        delay = 1
        logger = logging.Logger('manager.mqtt', level=logging.CRITICAL + 1)
        while True:
            root = self.root
            try:
                factory = self.factory or aiomqtt.Client
                async with factory(
                    hostname=self.config['host'], port=self.config['port'],
                    username=self.config['username'] or None, password=self.config['password'] or None,
                    identifier='tdm-' + self.instance_id, timeout=5, keepalive=30,
                    tls_context=ssl.create_default_context() if self.config['tls'] else None,
                    will=aiomqtt.Will(root + '/availability', 'offline', qos=1, retain=True),
                    max_queued_outgoing_messages=32, max_queued_incoming_messages=1,
                    max_concurrent_outgoing_calls=1, logger=logger,
                ) as client:
                    self.status['connection'] = 'connected'
                    delay = 1
                    sent = {}
                    try:
                        while True:
                            self.wake.clear()
                            await client.publish(root + '/availability', 'online', qos=1, retain=True)
                            for account_id, value in list(self.states.items()):
                                if account_id not in sent or sent[account_id] != value:
                                    payload = '' if value is None else json.dumps(value, separators=(',', ':'))
                                    await client.publish(root + '/' + account_id + '/state', payload, qos=1, retain=True)
                                    sent[account_id] = value
                            while self.events:
                                record = self.events[0]
                                created, account_id, event = record
                                if time.monotonic() - created > self.EVENT_TTL:
                                    self.events.popleft()
                                    self.status['dropped_events'] += 1
                                    continue
                                topic = root + '/' + account_id + '/events' if account_id else root + '/test'
                                await client.publish(topic, json.dumps(event, separators=(',', ':')), qos=1, retain=False)
                                if self.events and self.events[0] is record:
                                    self.events.popleft()
                            self.status['last_published_at'] = time.time()
                            try:
                                await asyncio.wait_for(self.wake.wait(), 30)
                            except asyncio.TimeoutError:
                                pass
                    finally:
                        try:
                            await asyncio.wait_for(client.publish(root + '/availability', 'offline', qos=1, retain=True), 2)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status['connection'] = 'connection failed; retrying'
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                self.status['connection'] = 'connection failed'
            self.task = None
