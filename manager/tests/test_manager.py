import asyncio
from pathlib import Path
import tempfile
import secrets
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, CookieJar, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from routing import rewrite_text, scope_cookie
from server import Manager, make_app
from vault import Vault
from mqtt_notifications import default_config


class RoutingTests(unittest.TestCase):
    def test_browser_assets_and_transports_use_account_paths(self):
        prefix = '/accounts/one'
        source = '''<script src="/static/app.js"></script>
fetch('/api/session'); fetch(`/api/history?${params}`);
const socket = io({transports:['websocket']});
path: 'api/session/vnc'; new URL('/api/session/vnc', location.href);
window.location.replace('/');
<script src="https://cdn.socket.io/socket.io.js"></script>'''
        rewritten = rewrite_text(source, prefix)
        self.assertIn('src="/accounts/one/static/app.js"', rewritten)
        self.assertIn("fetch('/accounts/one/api/session')", rewritten)
        self.assertIn('fetch(`/accounts/one/api/history?', rewritten)
        self.assertIn("path: '/accounts/one/socket.io'", rewritten)
        self.assertIn("path: 'accounts/one/api/session/vnc'", rewritten)
        self.assertIn("location.replace('/accounts/one/')", rewritten)
        self.assertIn('https://cdn.socket.io/socket.io.js', rewritten)

    def test_worker_cookies_are_path_scoped(self):
        cookie = scope_cookie('tdm_session=abc; HttpOnly; SameSite=strict; Path=/', '/accounts/one')[0]
        self.assertIn('Path=/accounts/one/', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=strict', cookie)

    def test_empty_strings_and_comments_are_not_url_rewritten(self):
        source = "this.msg = ''/*Z_NULL*/; const root = '/';"
        self.assertEqual(rewrite_text(source, '/accounts/one'),
                         "this.msg = ''/*Z_NULL*/; const root = '/accounts/one/';")


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime_patch = patch('server.RUNTIME', self.root / 'runtime')
        self.runtime_patch.start()
        self.vault = Vault(self.root / 'persistent', Fernet.generate_key())
        self.manager = Manager(vault=self.vault, password='test-password-for-dashboard', enforce_tmpfs=False)
        self.spawned = []

        async def spawn(account):
            self.spawned.append(account['id'])
            (self.root / 'runtime' / account['id'] / 'data').mkdir(parents=True, exist_ok=True)
        self.manager.spawn = spawn
        self.client = TestClient(TestServer(make_app(self.manager)), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/')).rstrip('/')
        self.headers = {'Origin': self.origin, 'X-Manager-Request': '1'}

    async def asyncTearDown(self):
        await self.client.close()
        self.runtime_patch.stop()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post('/_manager/login', headers=self.headers,
                                        json={'password': 'test-password-for-dashboard'})
        self.assertEqual(result.status, 200)
        self.assertIn('HttpOnly', result.headers.get('Set-Cookie', ''))

    async def test_dashboard_and_worker_require_authentication(self):
        result = await self.client.get('/_manager/api/accounts')
        self.assertEqual(result.status, 401)
        result = await self.client.get('/accounts/nonexistent/', allow_redirects=False)
        self.assertEqual(result.status, 302)
        result = await self.client.get('/_manager/health')
        self.assertEqual(result.status, 200)

    async def test_write_requires_same_origin_and_request_header(self):
        await self.login()
        for headers in ({}, {'Origin': 'http://attacker.invalid', 'X-Manager-Request': '1'}):
            result = await self.client.post('/_manager/api/accounts', headers=headers, json={'name': 'No'})
            self.assertEqual(result.status, 403)
        self.assertEqual(self.manager.accounts, [])

    async def test_global_theme_is_authenticated_saved_and_survives_restart(self):
        result = await self.client.get('/_manager/api/settings')
        self.assertEqual(result.status, 401)
        await self.login()
        result = await self.client.post('/_manager/api/settings', headers=self.headers, json={'theme': 'light'})
        self.assertEqual(result.status, 200)
        restarted = Manager(vault=self.vault, password='test-password-for-dashboard', enforce_tmpfs=False)
        self.assertEqual(restarted.preferences, {'theme': 'light'})
        result = await self.client.post('/_manager/api/settings', headers=self.headers, json={'theme': 'invalid'})
        self.assertEqual(result.status, 400)
        with patch.object(self.vault, 'save', side_effect=OSError):
            result = await self.client.post('/_manager/api/settings', headers=self.headers, json={'theme': 'dark'})
        self.assertEqual(result.status, 507)
        self.assertEqual(self.manager.preferences['theme'], 'light')

    async def test_notifications_require_auth_redact_password_and_persist(self):
        response = await self.client.get('/_manager/api/notifications')
        self.assertEqual(response.status, 401)
        await self.login()
        config = default_config()
        config['mqtt'].update(host='localhost', password='synthetic-integration-secret')
        self.assertEqual((await self.client.post('/_manager/api/notifications', json=config)).status, 403)
        response = await self.client.post('/_manager/api/notifications', headers=self.headers, json=config)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertNotIn('synthetic-integration-secret', text)
        self.assertTrue((await response.json())['settings']['mqtt']['password_set'])
        restarted = Manager(vault=self.vault, password='test-password-for-dashboard', enforce_tmpfs=False)
        self.assertEqual(restarted.notifications.config['mqtt']['password'], 'synthetic-integration-secret')
        response = await self.client.post('/_manager/api/notifications/test', headers=self.headers)
        self.assertEqual(response.status, 409)

    async def test_invalid_notification_settings_and_failed_save_keep_previous_config(self):
        await self.login()
        config = default_config()
        config['mqtt']['port'] = True
        response = await self.client.post('/_manager/api/notifications', headers=self.headers, json=config)
        self.assertEqual(response.status, 400)
        config = default_config()
        config['mqtt']['username'] = 'new-user'
        with patch.object(self.vault, 'save', side_effect=OSError):
            response = await self.client.post('/_manager/api/notifications', headers=self.headers, json=config)
        self.assertEqual(response.status, 507)
        self.assertEqual(self.manager.notifications.config['mqtt']['username'], '')

    async def test_icons_are_public_without_exposing_account_data(self):
        for path, kind in [('/_manager/icon.svg', 'image/svg+xml'), ('/_manager/icon.png', 'image/png'), ('/favicon.ico', 'image/png')]:
            result = await self.client.get(path)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.content_type, kind)

    async def create_account(self, name):
        response = await self.client.post('/_manager/api/accounts', headers=self.headers, json={'name': name})
        self.assertEqual(response.status, 201)
        return (await response.json())['id']

    async def test_delete_account_requires_auth_origin_and_exact_confirmation(self):
        self.assertEqual((await self.client.delete('/_manager/api/accounts/unknown', headers=self.headers,
                                                  json={'confirm_name': 'One'})).status, 401)
        await self.login()
        account_id = await self.create_account('One')
        path = '/_manager/api/accounts/' + account_id
        self.assertEqual((await self.client.delete(path, json={'confirm_name': 'One'})).status, 403)
        self.assertEqual((await self.client.delete(path, headers=self.headers,
                                                  json={'confirm_name': 'one'})).status, 400)
        self.assertIsNotNone(self.manager.account(account_id))

    async def test_delete_stops_worker_revokes_helper_and_removes_only_selected_data(self):
        await self.login()
        first = await self.create_account('One')
        second = await self.create_account('Two')
        for account_id in (first, second):
            root = self.root / 'runtime' / account_id / 'data'
            (root / 'session.json').write_text('synthetic-session')
            self.vault.snapshot(account_id, root)
        class Process:
            returncode = None
            stopped = False
            def terminate(self):
                self.stopped = True
            async def wait(self):
                self.returncode = 0
        process = Process()
        self.manager.workers[first] = process
        with patch.object(self.manager.helper, 'revoke', new_callable=AsyncMock) as revoke:
            response = await self.client.delete('/_manager/api/accounts/' + first, headers=self.headers,
                                                json={'confirm_name': 'One'})
            self.assertEqual(response.status, 200)
            revoke.assert_awaited_once_with(account_id=first)
        self.assertTrue(process.stopped)
        self.assertFalse((self.root / 'runtime' / first).exists())
        self.assertIsNone(self.vault.load(first))
        self.assertIsNotNone(self.vault.load(second))
        self.assertEqual([a['id'] for a in self.vault.load('registry')], [second])
        self.assertTrue((self.root / 'runtime' / second / 'data/session.json').exists())

    async def test_delete_cleanup_failure_is_stopped_persisted_and_retryable(self):
        await self.login()
        account_id = await self.create_account('One')
        path = '/_manager/api/accounts/' + account_id
        with patch('server.shutil.rmtree', side_effect=OSError):
            response = await self.client.delete(path, headers=self.headers, json={'confirm_name': 'One'})
        self.assertEqual(response.status, 507)
        record = self.vault.load('registry')[0]
        self.assertTrue(record['deleting'])
        self.assertFalse(record['enabled'])
        self.assertEqual((await self.client.patch(path, headers=self.headers, json={'enabled': True})).status, 409)
        restarted = Manager(vault=self.vault, password='test-password-for-dashboard', enforce_tmpfs=False)
        restarted.spawn = AsyncMock()
        await restarted.start(None)
        try:
            restarted.spawn.assert_not_awaited()
        finally:
            await restarted.close(None)
        self.assertIsNone(self.vault.load(account_id))  # Tombstones never checkpoint credentials.
        self.assertEqual((await self.client.delete(path, headers=self.headers, json={'confirm_name': 'One'})).status, 200)
        self.assertEqual(self.vault.load('registry'), [])

    async def test_delete_registry_failures_preserve_retry_state(self):
        await self.login()
        account_id = await self.create_account('One')
        path = '/_manager/api/accounts/' + account_id
        with patch.object(self.vault, 'save', side_effect=OSError):
            response = await self.client.delete(path, headers=self.headers, json={'confirm_name': 'One'})
        self.assertEqual(response.status, 507)
        self.assertTrue(self.manager.account(account_id)['enabled'])
        self.assertNotIn('deleting', self.manager.account(account_id))
        original_save = self.vault.save
        count = 0
        def fail_final_registry(scope, value):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError
            return original_save(scope, value)
        with patch.object(self.vault, 'save', side_effect=fail_final_registry):
            response = await self.client.delete(path, headers=self.headers, json={'confirm_name': 'One'})
        self.assertEqual(response.status, 507)
        self.assertTrue(self.manager.account(account_id)['deleting'])
        self.assertTrue(self.vault.load('registry')[0]['deleting'])
        self.assertEqual((await self.client.delete(path, headers=self.headers, json={'confirm_name': 'One'})).status, 200)

    async def test_queued_delete_rechecks_revoked_dashboard_session(self):
        await self.login()
        account_id = await self.create_account('One')
        decoded = asyncio.Event()
        original_body = self.manager.password_body
        async def observe_body(request):
            result = await original_body(request)
            decoded.set()
            return result
        async def delete():
            return await self.client.delete('/_manager/api/accounts/' + account_id,
                                            headers=self.headers, json={'confirm_name': 'One'})
        with patch.object(self.manager, 'password_body', side_effect=observe_body):
            async with self.manager.lock:
                pending = asyncio.create_task(delete())
                await asyncio.wait_for(decoded.wait(), 2)
                self.manager.sessions.clear()
            result = await pending
        self.assertEqual(result.status, 401)
        self.assertTrue(self.manager.account(account_id)['enabled'])
        self.assertNotIn('deleting', self.manager.account(account_id))

    async def test_add_three_accounts_persist_and_keep_independent_settings(self):
        await self.login()
        ids = []
        for name in ('One', 'Two', 'Three'):
            result = await self.client.post('/_manager/api/accounts', headers=self.headers, json={'name': name})
            self.assertEqual(result.status, 201)
            ids.append((await result.json())['id'])
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len(set(a['port'] for a in self.manager.accounts)), 3)
        self.assertEqual(self.spawned, ids)
        for account_id, game in zip(ids, ('Game A', 'Game B', 'Game A')):
            data = self.root / 'runtime' / account_id / 'data'
            (data / 'settings.json').write_text(game)
            self.vault.snapshot(account_id, data)
        for account_id, game in zip(ids, ('Game A', 'Game B', 'Game A')):
            target = self.root / 'restored' / account_id
            self.vault.restore(account_id, target)
            self.assertEqual((target / 'settings.json').read_text(), game)
        restarted = Manager(vault=self.vault, password='test-password-for-dashboard', enforce_tmpfs=False)
        self.assertEqual([a['id'] for a in restarted.accounts], ids)
        result = await self.client.get('/_manager/api/accounts')
        self.assertNotIn('port', (await result.json())['accounts'][0])

    async def test_proxy_preserves_account_routing_and_live_websockets(self):
        await self.login()
        async def page(request):
            self.assertNotIn('drops_manager', request.headers.get('Cookie', ''))
            response = web.Response(text='<script src="/static/app.js"></script>', content_type='text/html')
            response.set_cookie('tdm_session', 'worker-cookie', httponly=True)
            return response
        async def websocket(request):
            connection = web.WebSocketResponse()
            await connection.prepare(request)
            async for message in connection:
                if message.type == WSMsgType.TEXT:
                    await connection.send_str('echo:' + message.data)
            return connection
        worker = web.Application()
        worker.router.add_get('/', page)
        worker.router.add_get('/socket.io/', websocket)
        backend = TestServer(worker)
        await backend.start_server()
        try:
            self.manager.accounts.append({'id': 'one', 'name': 'One', 'enabled': True, 'port': backend.port})
            response = await self.client.get('/accounts/one/')
            self.assertEqual(response.status, 200)
            self.assertIn('/accounts/one/static/app.js', await response.text())
            self.assertIn('Path=/accounts/one/', response.headers['Set-Cookie'])
            async with self.client.ws_connect('/accounts/one/socket.io/', headers={'Origin': self.origin}) as connection:
                await connection.send_str('hello')
                message = await asyncio.wait_for(connection.receive(), 3)
                self.assertEqual(message.data, 'echo:hello')
        finally:
            await backend.close()

    async def test_logout_invalidates_session(self):
        await self.login()
        result = await self.client.post('/_manager/logout', headers=self.headers)
        self.assertEqual(result.status, 200)
        result = await self.client.get('/_manager/api/accounts')
        self.assertEqual(result.status, 401)

    async def test_first_run_requires_password_and_persists_only_hash(self):
        self.manager.password = None
        self.assertFalse(self.manager.configured())
        result = await self.client.get('/', allow_redirects=False)
        self.assertEqual(result.headers['Location'], '/_manager/setup.html')
        result = await self.client.get('/_manager/api/accounts')
        self.assertEqual(result.status, 401)
        password = 'a-new-personal-dashboard-password'
        result = await self.client.post('/_manager/setup', headers=self.headers,
                                        json={'password': password, 'confirm_password': password})
        self.assertEqual(result.status, 200)
        self.assertTrue(self.manager.configured())
        self.assertIsNone(self.manager.password)
        self.assertTrue(await self.manager.matches_password(password))
        self.assertNotIn(password.encode(), (self.root / 'persistent' / 'dashboard-auth.enc').read_bytes())
        record = self.vault.load('dashboard-auth')
        self.assertEqual(set(record), {'version', 'salt', 'hash'})
        restarted = Manager(vault=self.vault, enforce_tmpfs=False)
        self.assertTrue(await restarted.matches_password(password))
        result = await self.client.get('/_manager/api/accounts')
        self.assertEqual(result.status, 200)
        result = await self.client.post('/_manager/setup', headers=self.headers,
                                        json={'password': password, 'confirm_password': password})
        self.assertEqual(result.status, 409)

    async def test_setup_validates_confirmation_length_and_origin(self):
        self.manager.password = None
        good = 'a-new-personal-dashboard-password'
        result = await self.client.post('/_manager/setup', json={'password': good, 'confirm_password': good})
        self.assertEqual(result.status, 403)
        for password, confirmation in [('short', 'short'), (good, 'does-not-match')]:
            result = await self.client.post('/_manager/setup', headers=self.headers,
                                            json={'password': password, 'confirm_password': confirmation})
            self.assertEqual(result.status, 400)
        self.assertFalse(self.manager.configured())

    async def test_concurrent_first_run_has_one_winner(self):
        self.manager.password = None
        password = 'a-new-personal-dashboard-password'
        results = await asyncio.gather(*(self.client.post('/_manager/setup', headers=self.headers,
                  json={'password': password, 'confirm_password': password}) for _ in range(2)))
        self.assertEqual(sorted(r.status for r in results), [200, 409])

    async def test_password_change_revokes_old_sessions_and_survives_restart(self):
        await self.login()
        old = self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))['drops_manager'].value
        password = 'a-different-personal-dashboard-password'
        result = await self.client.post('/_manager/password', headers=self.headers,
            json={'current_password': 'test-password-for-dashboard',
                  'password': password, 'confirm_password': password})
        self.assertEqual(result.status, 200)
        self.assertNotIn(old, self.manager.sessions)
        self.assertEqual((await self.client.get('/_manager/api/accounts')).status, 200)
        async with ClientSession() as stale:
            async with stale.get(str(self.client.make_url('/_manager/api/accounts')),
                                  headers={'Cookie': 'drops_manager=' + old}) as result:
                self.assertEqual(result.status, 401)
        self.assertFalse(await self.manager.matches_password('test-password-for-dashboard'))
        restarted = Manager(vault=self.vault, enforce_tmpfs=False)
        self.assertTrue(await restarted.matches_password(password))

    async def test_password_change_requires_current_password_and_session(self):
        password = 'a-different-personal-dashboard-password'
        body = {'current_password': 'wrong-password', 'password': password, 'confirm_password': password}
        result = await self.client.post('/_manager/password', headers=self.headers, json=body)
        self.assertEqual(result.status, 401)
        await self.login()
        result = await self.client.post('/_manager/password', headers=self.headers, json=body)
        self.assertEqual(result.status, 401)
        self.assertTrue(await self.manager.matches_password('test-password-for-dashboard'))

    async def test_failed_password_save_retains_previous_password_and_session(self):
        await self.login()
        password = 'a-different-personal-dashboard-password'
        with patch.object(self.vault, 'save', side_effect=OSError('synthetic disk-full error')):
            result = await self.client.post('/_manager/password', headers=self.headers,
                json={'current_password': 'test-password-for-dashboard',
                      'password': password, 'confirm_password': password})
        self.assertEqual(result.status, 507)
        self.assertTrue(await self.manager.matches_password('test-password-for-dashboard'))
        self.assertEqual((await self.client.get('/_manager/api/accounts')).status, 200)

    async def fake_helper(self, account_id):
        state = {'enabled': False, 'ticket': None, 'uploads': [], 'cancels': 0, 'complete': False}

        async def control(request):
            state['enabled'] = request.path.endswith('/enable')
            state['ticket'] = None
            state['complete'] = False
            if not state['enabled']:
                state['cancels'] += 1
            return web.json_response({'helper': {'state': 'waiting' if state['enabled'] else 'disabled'}})

        async def native(request):
            self.assertEqual(request.headers.get('X-TDM-Request'), '1')
            self.assertNotIn('drops_manager', request.headers.get('Cookie', ''))
            if request.path.endswith('/connect'):
                if not state['enabled'] or state['ticket']:
                    return web.json_response({'detail': 'session_busy'}, status=409)
                state['ticket'] = secrets.token_urlsafe(32)
                return web.json_response({'version': 1, 'connection': state['ticket'], 'expires_at': time.time() + 600})
            if not state['ticket'] or request.headers.get('Authorization') != 'Bearer ' + state['ticket']:
                return web.json_response({'detail': 'session_connection'}, status=403)
            if request.path.endswith('/session'):
                state['uploads'].append(await request.json())
                state['complete'] = True
                return web.json_response({'state': 'complete'})
            return web.json_response({'state': 'pending'})

        app = web.Application()
        async def status(request):
            return web.json_response({'logged_in': state['complete'],
                                      'helper': {'state': 'complete' if state['complete'] else 'waiting'}})
        app.router.add_get('/api/session', status)
        app.router.add_post('/api/session/helper/enable', control)
        app.router.add_post('/api/session/helper/cancel', control)
        app.router.add_post('/api/helper/connect', native)
        app.router.add_post('/api/helper/session', native)
        app.router.add_get('/api/helper/result', native)
        backend = TestServer(app)
        await backend.start_server()
        self.addAsyncCleanup(backend.close)
        self.manager.accounts.append({'id': account_id, 'name': account_id, 'enabled': True, 'port': backend.port})
        return state

    async def enable_helper(self, account_id):
        return await self.client.post('/accounts/' + account_id + '/api/session/helper/enable',
                                      headers={**self.headers, 'X-TDM-Request': '1'}, json={})

    async def test_native_helper_is_closed_without_explicit_dashboard_admission(self):
        await self.fake_helper('one')
        for method, path in [('POST', 'connect'), ('POST', 'session'), ('GET', 'result')]:
            result = await self.client.request(method, '/api/helper/' + path,
                                                headers={'X-TDM-Request': '1'})
            self.assertEqual(result.status, 403)
        result = await self.client.post('/accounts/one/api/session/helper/enable',
                                        headers={'X-TDM-Request': '1', 'Origin': self.origin},
                                        json={}, allow_redirects=False)
        self.assertEqual(result.status, 302)
        self.assertIsNone(self.manager.helper.live())

    async def test_native_helper_stays_bound_to_one_account_and_keeps_bearer_auth(self):
        await self.login()
        first = await self.fake_helper('one')
        second = await self.fake_helper('two')
        self.assertEqual((await self.enable_helper('one')).status, 200)
        self.assertEqual((await self.enable_helper('two')).status, 409)
        self.assertFalse(second['enabled'])
        # Desktop helpers have no dashboard cookie or browser Origin header.
        async with ClientSession() as native:
            url = str(self.client.make_url('/api/helper/connect'))
            async with native.post(url, headers={'X-TDM-Request': '1'}, json={}) as result:
                self.assertEqual(result.status, 200)
                ticket = (await result.json())['connection']
            async with native.post(url, headers={'X-TDM-Request': '1'}, json={}) as result:
                self.assertEqual(result.status, 409)
            target = str(self.client.make_url('/api/helper/session'))
            async with native.post(target, headers={'X-TDM-Request': '1', 'Authorization': 'Bearer wrong'}, json={}) as result:
                self.assertEqual(result.status, 403)
            async with native.post(target, headers={'X-TDM-Request': '1', 'Authorization': 'Bearer ' + ticket},
                                   json={'synthetic': 'first-account'}) as result:
                self.assertEqual(result.status, 200)
        self.assertEqual(first['uploads'], [{'synthetic': 'first-account'}])
        self.assertEqual(second['uploads'], [])
        self.assertEqual((await self.enable_helper('two')).status, 200)
        self.assertEqual(self.manager.helper.live()['account'], 'two')

    async def test_helper_rejects_cross_site_and_headerless_requests(self):
        await self.login()
        await self.fake_helper('one')
        self.assertEqual((await self.enable_helper('one')).status, 200)
        for headers in ({}, {'X-TDM-Request': '1', 'Origin': 'http://evil.invalid'},
                        {'X-TDM-Request': '1', 'Sec-Fetch-Site': 'cross-site'}):
            result = await self.client.post('/api/helper/connect', headers=headers, json={})
            self.assertEqual(result.status, 403)

    async def test_helper_cancel_expiry_and_logout_revoke_admission(self):
        await self.login()
        state = await self.fake_helper('one')
        self.assertEqual((await self.enable_helper('one')).status, 200)
        result = await self.client.post('/accounts/one/api/session/helper/cancel',
                                        headers={**self.headers, 'X-TDM-Request': '1'}, json={})
        self.assertEqual(result.status, 200)
        self.assertIsNone(self.manager.helper.live())
        self.assertFalse(state['enabled'])
        self.assertEqual((await self.enable_helper('one')).status, 200)
        self.manager.helper.window['until'] = time.time() - 1
        await self.manager.helper.expire()
        self.assertIsNone(self.manager.helper.live())
        self.assertFalse(state['enabled'])
        self.assertEqual((await self.enable_helper('one')).status, 200)
        result = await self.client.post('/_manager/logout', headers=self.headers)
        self.assertEqual(result.status, 200)
        self.assertIsNone(self.manager.helper.live())
        self.assertFalse(state['enabled'])
