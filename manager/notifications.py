"""Global notification settings, durable observations, and account event routing."""
import asyncio
import copy
import secrets
import time
from collections import deque

from aiohttp import ClientTimeout

from mqtt_notifications import EVENTS, MqttPublisher, default_config, validate_config


class NotificationHub:
    def __init__(self, manager):
        self.manager = manager
        self.config = manager.vault.load('notification-settings', default_config())
        self.config['accounts'] = {key: value for key, value in self.config['accounts'].items()
                                   if manager.account(key) is not None}
        self.config = validate_config(self.config, default_config(), [a['id'] for a in manager.accounts])
        self.saved = manager.vault.load('notification-state', {
            'instance_id': secrets.token_hex(6), 'accounts': {}, 'recent': []})
        self.saved['accounts'] = {key: value for key, value in self.saved['accounts'].items()
                                   if manager.account(key) is not None}
        self.publisher = MqttPublisher(self.saved['instance_id'])
        self.observations = {}
        self.recent = deque((event for event in self.saved.get('recent', [])
                             if manager.account(event['account_id']) is not None), maxlen=100)
        self.queue = asyncio.Queue(maxsize=64)
        self.tasks = []
        self.lock = asyncio.Lock()
        self.error = None

    def public(self):
        config = copy.deepcopy(self.config)
        config['mqtt']['password_set'] = bool(config['mqtt'].pop('password'))
        return {'settings': config, 'mqtt': {**self.publisher.status, 'topic_root': self.publisher.root},
                'error': self.error, 'accounts': self.observations, 'recent': list(self.recent)[-20:]}

    async def configure(self, value):
        async with self.lock:
            config = validate_config(value, self.config, [a['id'] for a in self.manager.accounts])
            self.manager.vault.save('notification-settings', config)
            broker_changed = config['mqtt'] != self.config['mqtt']
            self.config = config
            if broker_changed:
                await self.publisher.configure(config['mqtt'])
            else:
                self.publisher.events = deque(record for record in self.publisher.events
                    if not record[1] or (self.rule(record[1])['mqtt']
                        and self.config['events'].get(record[2]['event'], False)
                        and record[2]['event'] in self.rule(record[1])['events']))
            # Apply changed account routing to already-retained states too.
            for account in self.manager.accounts:
                state = self.observations.get(account['id'])
                if state:
                    self.publish_state(account, state)
        return self.public()

    def persist(self):
        self.saved['recent'] = list(self.recent)
        self.manager.vault.save('notification-state', self.saved)

    async def start(self):
        try:
            self.persist()
        except OSError:
            self.error = 'Could not save notification observations'
        await self.publisher.configure(self.config['mqtt'])
        self.tasks = [asyncio.create_task(self.poll()), asyncio.create_task(self.deliver())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self.publisher.close()

    def rule(self, account_id):
        return self.config['accounts'].get(account_id, {
            'telegram': True, 'discord': True, 'mqtt': True, 'events': list(EVENTS)})

    def publish_state(self, account, state):
        self.publisher.state(account['id'], state if self.rule(account['id'])['mqtt'] else None)

    async def forget(self, account_id):
        async with self.lock:
            self.saved['accounts'].pop(account_id, None)
            self.observations.pop(account_id, None)
            self.config['accounts'].pop(account_id, None)
            self.recent = deque((e for e in self.recent if e['account_id'] != account_id), maxlen=100)
            self.publisher.state(account_id, None)
            self.publisher.events = deque(record for record in self.publisher.events if record[1] != account_id)
            try:
                self.persist()
                self.manager.vault.save('notification-settings', self.config)
            except OSError:
                self.error = 'Could not save notification cleanup'

    async def poll(self):
        await asyncio.sleep(1)
        while True:
            for account in list(self.manager.accounts):
                if account.get('deleting'):
                    continue
                if not account['enabled']:
                    state = {'account_id': account['id'], 'account': account['name'],
                             'status': 'stopped', 'observed_at': time.time(), 'alerts': []}
                    self.observations[account['id']] = state
                    self.publish_state(account, state)
                    continue
                try:
                    async with self.manager.client.get(
                        f"http://127.0.0.1:{account['port']}/api/manager/notifications",
                        timeout=ClientTimeout(total=3)) as response:
                        if response.status != 200:
                            raise ValueError('unavailable')
                        data = await response.json()
                    async with self.lock:
                        if self.manager.account(account['id']) is not account or not account['enabled'] or account.get('deleting'):
                            continue
                        self.observe(account, data)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A timeout/unsupported worker is unknown, never "not linked".
                    if self.manager.account(account['id']) is account:
                        previous = self.observations.get(account['id'], {})
                        state = {**previous, 'account_id': account['id'], 'account': account['name'],
                                 'status': 'unavailable', 'checked_at': time.time()}
                        self.observations[account['id']] = state
                        self.publish_state(account, state)
            await asyncio.sleep(30)

    def observe(self, account, data):
        if type(data.get('logged_in')) is not bool:
            raise ValueError('Invalid worker observation')
        account_id = account['id']
        old = self.saved['accounts'].get(account_id, {})
        current = copy.deepcopy(old)
        events = []
        def emit(kind, **details):
            events.append({'event': kind, 'event_id': secrets.token_hex(12), 'timestamp': time.time(),
                           'account_id': account_id, 'account': account['name'], **details})
        logged_in = data['logged_in']
        if not logged_in and old.get('logged_in') is not False:
            emit('login_required')
        current['logged_in'] = logged_in
        error = data.get('renewal_error')
        if error is not None and (not isinstance(error, str) or not error.isupper() or len(error) > 64):
            raise ValueError('Invalid renewal status')
        if error and error != old.get('renewal_error'):
            emit('renewal_failed', code=error)
        current['renewal_error'] = error
        alerts = self.observations.get(account_id, {}).get('alerts', [])
        observed = data.get('observed_at')
        interval = data.get('refresh_minutes', 30)
        fresh = (isinstance(observed, (float, int)) and type(interval) is int
                 and 0 <= time.time() - observed <= max(300, interval * 120 + 120))
        if logged_in and data.get('inventory_ready') is True and fresh:
            links = current.setdefault('links', {})
            alerts = []
            for campaign in data['campaigns'][:500]:
                if type(campaign.get('linked')) is not bool:
                    raise ValueError('Invalid campaign link status')
                fields = {k: campaign.get(k) for k in ('id', 'game', 'campaign', 'link_url')}
                if any(not isinstance(fields[k], str) or len(fields[k]) > 256 for k in ('id', 'game', 'campaign')):
                    raise ValueError('Invalid campaign')
                from urllib.parse import urlsplit
                link = fields['link_url']
                if link:
                    url = urlsplit(link)
                    if (len(link) > 2048 or url.scheme != 'https' or not url.hostname
                            or url.username or url.password or url.query or url.fragment):
                        fields['link_url'] = None
                previous = links.get(fields['id'])
                linked = campaign['linked']
                if not linked:
                    alerts.append(fields)
                    if previous is not False:
                        emit('account_link_required', **{k: fields[k] for k in ('game', 'campaign', 'link_url')},
                             change='lost' if previous is True else 'unlinked')
                elif previous is False:
                    emit('account_link_restored', game=fields['game'], campaign=fields['campaign'])
                links[fields['id']] = linked
            current['links'] = dict(list(links.items())[-500:])
        if logged_in:
            claims = data.get('claims', [])[:2000]
            seen = set(old.get('claims', []))
            for claim in claims:
                if any(not isinstance(claim.get(k), str) or len(claim[k]) > 256 for k in ('id', 'game', 'campaign', 'reward')):
                    raise ValueError('Invalid claim observation')
                if old.get('claims_initialized') and claim['id'] not in seen:
                    emit('drop_claimed', **{k: claim[k] for k in ('game', 'campaign', 'reward')})
                seen.add(claim['id'])
            current['claims'] = list(dict.fromkeys(old.get('claims', []) + [c['id'] for c in reversed(claims)]))[-2000:]
            current['claims_initialized'] = True
        rule = self.rule(account_id)
        selected = [event for event in events if self.config['events'][event['event']] and event['event'] in rule['events']]
        previous_recent = deque(self.recent, maxlen=100)
        self.recent.extend(selected)
        if current != old:
            self.saved['accounts'][account_id] = current
            try:
                self.persist()
                self.error = None
            except OSError:
                self.recent = previous_recent
                if old:
                    self.saved['accounts'][account_id] = old
                else:
                    self.saved['accounts'].pop(account_id, None)
                self.error = 'Could not save notification observations'
                return
        state = {'account_id': account_id, 'account': account['name'],
                 'status': 'signed_in' if logged_in else 'login_required',
                 'checked_at': time.time(), 'inventory_observed_at': observed,
                 'link_status_fresh': bool(fresh and data.get('inventory_ready')), 'alerts': alerts,
                 'renewal_error': error}
        self.observations[account_id] = state
        self.publish_state(account, state)
        for event in selected:
            if rule['mqtt']:
                self.publisher.event(account_id, event)
        # Native claim notifications already exist; do not duplicate them.
        native = [event for event in selected if event['event'] != 'drop_claimed']
        routes = {key: bool(rule[key] and data.get('channels', {}).get(key)) for key in ('telegram', 'discord')}
        for start in range(0, len(native), 10):
            if any(routes.values()):
                try:
                    self.queue.put_nowait((account_id, time.monotonic(), routes, native[start:start + 10]))
                except asyncio.QueueFull:
                    self.error = 'Notification delivery queue is full'

    async def deliver(self):
        while True:
            account_id, created, routes, events = await self.queue.get()
            account = self.manager.account(account_id)
            if account is None or not account['enabled'] or account.get('deleting') or time.monotonic() - created > 3600:
                continue
            rule = self.rule(account_id)
            events = [e for e in events if self.config['events'][e['event']] and e['event'] in rule['events']]
            if not events:
                continue
            body = {'account': account['name'], 'events': events,
                    **{key: bool(routes[key] and rule[key]) for key in ('telegram', 'discord')}}
            try:
                origin = f"http://127.0.0.1:{account['port']}"
                async with self.manager.client.post(origin + '/api/manager/notifications', json=body,
                        headers={'X-TDM-Request': '1', 'Origin': self.manager.public_origin or origin},
                        timeout=ClientTimeout(total=25)) as response:
                    if response.status != 200:
                        self.error = 'Account notification delivery failed'
                    else:
                        result = await response.json()
                        if any(body[key] and result.get(key) is not True for key in ('telegram', 'discord')):
                            self.error = 'Account notification delivery failed'
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = 'Account notification delivery failed'
