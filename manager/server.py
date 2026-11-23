"""Single-port, password-protected account manager and miner reverse proxy."""
import asyncio
import contextlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import sys
import time
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from multidict import CIMultiDict

from routing import rewrite_text, scope_cookie
from helper_bridge import HelperBridge, NATIVE_ROUTES
from vault import Vault
from passwords import hash_password, verify_password
from notifications import NotificationHub

UI = Path(__file__).parent / 'web'
RUNTIME = Path('/runtime')
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length'}
COOKIE = 'drops_manager'


class Manager:
    def __init__(self, vault=None, password=None, enforce_tmpfs=True):
        os.umask(0o077)
        if enforce_tmpfs:
            mounts = Path('/proc/self/mountinfo').read_text().splitlines()
            for target in ('/runtime', '/tmp'):
                if not any(line.split()[4] == target and line.split(' - ')[1].split()[0] == 'tmpfs' for line in mounts):
                    raise ValueError(f'{target} must be mounted as tmpfs; refusing persistent plaintext storage')
            if Path('/run/secrets').stat().st_mode & 0o077:
                raise ValueError('Secret directory must be accessible only to root')
        self.vault = vault or Vault(Path('/data'), Path('/run/secrets/account_key').read_bytes())
        self.password_record = self.vault.load('dashboard-auth')
        legacy_file = Path('/run/secrets/dashboard_password')
        self.password = password if password is not None else (
            legacy_file.read_text().strip() if self.password_record is None and legacy_file.is_file() else None)
        if self.password is not None and len(self.password) < 16:
            raise ValueError('Dashboard secret must contain at least 16 characters')
        if self.password_record is not None:
            verify_password('', self.password_record)  # Validate stored format; fail closed.
        self.accounts = self.vault.load('registry', [])
        self.preferences = self.vault.load('manager-settings', {'theme': 'dark'})
        self.workers = {}
        self.failures = {}
        self.sessions = {}
        self.attempts = []
        self.lock = asyncio.Lock()
        self.password_lock = asyncio.Lock()
        self.client = None
        self.monitor = None
        self.closing = False
        self.storage_error = False
        self.helper = HelperBridge(self)
        self.public_origin = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')
        if self.public_origin:
            url = urlsplit(self.public_origin)
            if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                    or url.password or url.path or url.query or url.fragment):
                raise ValueError('PUBLIC_BASE_URL must be an http(s) root URL')
        self.notifications = NotificationHub(self)

    def authenticated(self, request):
        return self.sessions.get(request.cookies.get(COOKIE, ''), 0) > time.time()

    def configured(self):
        return self.password_record is not None or self.password is not None

    async def matches_password(self, password):
        if self.password_record is not None:
            return await asyncio.to_thread(verify_password, password, self.password_record)
        return self.password is not None and hmac.compare_digest(password.encode(), self.password.encode())

    def session_response(self):
        token = secrets.token_urlsafe(32)
        self.sessions[token] = time.time() + 12 * 3600
        response = web.json_response({'ok': True})
        return response, token

    def set_session_cookie(self, response, token, request):
        response.set_cookie(COOKIE, token, httponly=True, samesite='Strict',
                            secure=self.public_origin.startswith('https://') or request.secure,
                            max_age=12 * 3600, path='/')

    def limit_password_attempts(self):
        now = time.time()
        self.attempts = [t for t in self.attempts if t > now - 60]
        if len(self.attempts) >= 10:
            raise web.HTTPTooManyRequests(text='Try again in a minute')
        self.attempts.append(now)

    async def password_body(self, request):
        if request.content_length and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)
        raw = await request.read()
        if len(raw) > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=len(raw))
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text='Invalid request')
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='Invalid request')
        return body

    async def set_password(self, request):
        first_run = request.path == '/_manager/setup'
        async with self.password_lock:
            if first_run and self.configured():
                raise web.HTTPConflict(text='Dashboard password is already configured')
            if not first_run and not self.authenticated(request):
                raise web.HTTPUnauthorized()
            self.limit_password_attempts()
            body = await self.password_body(request)
            if not first_run:
                current = body.get('current_password')
                if not isinstance(current, str) or not await self.matches_password(current):
                    raise web.HTTPUnauthorized(text='Current password is incorrect')
            password, confirmation = body.get('password'), body.get('confirm_password')
            if not isinstance(password, str) or not 16 <= len(password) <= 1024:
                raise web.HTTPBadRequest(text='Use a password of 16–1024 characters')
            if password != confirmation:
                raise web.HTTPBadRequest(text='New passwords do not match')
            record = await asyncio.to_thread(hash_password, password)
            try:
                self.vault.save('dashboard-auth', record)
            except OSError:
                raise web.HTTPInsufficientStorage(text='Could not save the password; previous password retained')
            self.password_record, self.password = record, None
            self.sessions.clear()
            self.attempts.clear()
            await self.helper.revoke()
            response, token = self.session_response()
            self.set_session_cookie(response, token, request)
            return response

    def account(self, account_id):
        return next((a for a in self.accounts if a['id'] == account_id), None)

    def save_registry(self):
        self.vault.save('registry', self.accounts)

    async def settings(self, request):
        if request.method == 'GET':
            return web.json_response(self.preferences)
        body = await self.password_body(request)
        if set(body) != {'theme'} or body['theme'] not in ('dark', 'light'):
            raise web.HTTPBadRequest(text='Choose dark or light theme')
        async with self.lock:
            try:
                self.vault.save('manager-settings', body)
            except OSError:
                raise web.HTTPInsufficientStorage(text='Could not save theme; previous setting retained')
            self.preferences = body
        return web.json_response(self.preferences)

    async def start(self, app):
        self.client = ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False,
                                    timeout=ClientTimeout(total=None, sock_connect=5, sock_read=120))
        for account in self.accounts:
            if account.get('deleting'):
                continue
            self.vault.restore(account['id'], RUNTIME / account['id'] / 'data')
            if account['enabled']:
                await self.spawn(account)
        self.monitor = asyncio.create_task(self.supervise())
        await self.notifications.start()

    async def spawn(self, account):
        root = RUNTIME / account['id']
        (root / 'data').mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {**os.environ, 'TDM_DATA_DIR': str(root / 'data'), 'TDM_PORT': str(account['port'])}
        self.workers[account['id']] = await asyncio.create_subprocess_exec(
            sys.executable, '/app/main.py', cwd=root, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    async def stop(self, account):
        await self.helper.revoke(account_id=account['id'])
        process = self.workers.pop(account['id'], None)
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 35)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if not account.get('deleting'):
            self.vault.snapshot(account['id'], RUNTIME / account['id'] / 'data')

    async def supervise(self):
        cycle = 0
        while True:
            await asyncio.sleep(5)
            async with self.lock:
                await self.helper.expire()
                for account in self.accounts:
                    worker = self.workers.get(account['id'])
                    if account['enabled'] and (not worker or worker.returncode is not None):
                        count, retry = self.failures.get(account['id'], (0, 0))
                        if time.monotonic() >= retry:
                            self.failures[account['id']] = (count + 1, time.monotonic() + min(300, 5 * 2 ** min(count, 6)))
                            await self.spawn(account)
                cycle += 1
                if cycle % 3 == 0:
                    try:
                        for account in self.accounts:
                            if not account.get('deleting'):
                                self.vault.snapshot(account['id'], RUNTIME / account['id'] / 'data')
                        self.storage_error = False
                    except Exception:
                        self.storage_error = True
                        print('Encrypted checkpoint failed; check storage availability.', flush=True)
            now = time.time()
            self.sessions = {token: expiry for token, expiry in self.sessions.items() if expiry > now}

    async def close(self, app):
        self.closing = True
        await self.notifications.close()
        if self.monitor:
            self.monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor
        results = await asyncio.gather(*(self.stop(a) for a in self.accounts), return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            print('Final encrypted checkpoint failed; last successful checkpoint retained.', flush=True)
        if self.client:
            await self.client.close()

    async def summary(self, account):
        worker = self.workers.get(account['id'])
        status = 'stopped' if not account['enabled'] else 'starting'
        if worker and worker.returncode is None:
            try:
                async with self.client.get(f"http://127.0.0.1:{account['port']}/healthz",
                                           timeout=ClientTimeout(total=2)) as response:
                    if response.status == 200:
                        status = 'running'
            except (OSError, asyncio.TimeoutError):
                pass
        elif account['enabled']:
            status = 'restarting'
        if account.get('deleting'):
            status = 'deletion pending'
        return {'id': account['id'], 'name': account['name'], 'enabled': account['enabled'], 'status': status,
                'deleting': bool(account.get('deleting'))}

    async def delete_account(self, request):
        body = await self.password_body(request)
        async with self.lock:
            if not self.authenticated(request):
                raise web.HTTPUnauthorized()
            account = self.account(request.match_info['account'])
            if not account:
                raise web.HTTPNotFound()
            if body.get('confirm_name') != account['name']:
                raise web.HTTPBadRequest(text='Type the account name exactly to confirm deletion')
            original = account.copy()
            account.update(enabled=False, deleting=True)
            try:
                # A committed tombstone prevents restart/checkpoint from restoring a
                # partially deleted account after an interrupted cleanup.
                self.save_registry()
            except OSError:
                account.clear()
                account.update(original)
                raise web.HTTPInsufficientStorage(text='Could not save deletion request; account retained')
            try:
                await self.stop(account)  # Cancels helper access and stops its worker.
                root = RUNTIME / account['id']
                if root.resolve().parent != RUNTIME.resolve():
                    raise OSError('Invalid runtime directory')
                if root.exists():
                    shutil.rmtree(root)
                self.vault.delete(account['id'])
                index = self.accounts.index(account)
                self.accounts.pop(index)
                try:
                    self.save_registry()
                except OSError:
                    self.accounts.insert(index, account)
                    raise
            except OSError:
                raise web.HTTPInsufficientStorage(text='Deletion incomplete. Account is stopped; retry Delete account to finish cleanup.')
            self.failures.pop(account['id'], None)
            await self.notifications.forget(account['id'])
            return web.json_response({'ok': True})

    async def notification_settings(self, request):
        if request.method == 'GET':
            return web.json_response(self.notifications.public())
        body = await self.password_body(request)
        async with self.lock:
            if not self.authenticated(request):
                raise web.HTTPUnauthorized()
            try:
                return web.json_response(await self.notifications.configure(body))
            except ValueError as error:
                raise web.HTTPBadRequest(text=str(error))
            except OSError:
                raise web.HTTPInsufficientStorage(text='Could not save notification settings')

    async def notification_test(self, request):
        async with self.notifications.lock:
            if not self.authenticated(request):
                raise web.HTTPUnauthorized()
            publisher = self.notifications.publisher
            if not publisher.config['enabled']:
                raise web.HTTPConflict(text='Save an enabled MQTT connection first')
            publisher.event('', {'event': 'test', 'event_id': secrets.token_hex(12), 'timestamp': time.time()})
        return web.json_response({'queued': True}, status=202)

    async def api(self, request):
        if request.method == 'GET':
            return web.json_response({'accounts': await asyncio.gather(*(self.summary(a) for a in self.accounts)),
                                      'storage_error': self.storage_error,
                                      'helper_account': (self.helper.live() or {}).get('account')})
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeError):
            raise web.HTTPBadRequest(text='Invalid JSON')
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='Expected an object')
        async with self.lock:
            if request.method == 'POST':
                name = body.get('name', '')
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                    raise web.HTTPBadRequest(text='Use an account name of 1–80 characters')
                ports = {a['port'] for a in self.accounts}
                port = next((p for p in range(12000, 60000) if p not in ports), None)
                if port is None:
                    raise web.HTTPConflict(text='No worker ports available')
                account = {'id': secrets.token_hex(8), 'name': name.strip(), 'port': port, 'enabled': True}
                self.accounts.append(account)
                try:
                    self.save_registry()
                except Exception:
                    self.accounts.remove(account)
                    raise web.HTTPInsufficientStorage(text='Could not save encrypted registry')
                await self.spawn(account)
                return web.json_response({'id': account['id']}, status=201)
            account = self.account(request.match_info['account'])
            if not account:
                raise web.HTTPNotFound()
            if account.get('deleting'):
                raise web.HTTPConflict(text='Account deletion is pending; retry Delete account')
            if 'name' in body:
                name = body['name']
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                    raise web.HTTPBadRequest(text='Invalid account name')
            if 'enabled' in body and type(body['enabled']) is not bool:
                raise web.HTTPBadRequest(text='Invalid enabled flag')
            old = account.copy()
            if 'name' in body:
                account['name'] = body['name'].strip()
            if 'enabled' in body:
                account['enabled'] = body['enabled']
            try:
                self.save_registry()
            except Exception:
                account.update(old)
                raise web.HTTPInsufficientStorage(text='Could not save encrypted registry')
            if old['enabled'] and not account['enabled']:
                await self.stop(account)
            elif account['enabled'] and not old['enabled']:
                await self.spawn(account)
            return web.json_response({'ok': True})

    async def login(self, request):
        async with self.password_lock:
            return await self._login(request)

    async def _login(self, request):
        if not self.configured():
            raise web.HTTPConflict(text='Create the dashboard password first')
        self.limit_password_attempts()
        body = await self.password_body(request)
        password = body.get('password') if isinstance(body, dict) else None
        if not isinstance(password, str) or len(password) > 1024 or not await self.matches_password(password):
            raise web.HTTPUnauthorized(text='Incorrect dashboard password')
        self.attempts.clear()
        response, token = self.session_response()
        self.set_session_cookie(response, token, request)
        return response

    async def proxy(self, request, account_id=None, tail=None):
        account = self.account(account_id or request.match_info['account'])
        if not account:
            raise web.HTTPNotFound()
        if not account['enabled']:
            return web.Response(text='This account is stopped. Start it from the account sidebar.', status=503)
        prefix = '/accounts/' + account['id']
        tail = request.match_info.get('tail', '') if tail is None else tail
        url = f"http://127.0.0.1:{account['port']}/" + tail
        if request.query_string:
            url += '?' + request.query_string
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | {'accept-encoding'}}
        # The private manager session must never enter a miner process.
        headers['Cookie'] = '; '.join(f'{k}={v}' for k, v in request.cookies.items() if k != COOKIE)
        try:
            if request.headers.get('Upgrade', '').lower() == 'websocket':
                return await self.websocket(request, url, headers)
            async with self.client.request(request.method, url, data=await request.read(), headers=headers,
                                           allow_redirects=False) as upstream:
                outgoing = CIMultiDict()
                for key, value in upstream.headers.items():
                    if key.lower() in HOP | {'set-cookie'}:
                        continue
                    if key.lower() == 'location' and value.startswith('/'):
                        value = prefix + value
                    outgoing.add(key, value)
                for cookie in upstream.headers.getall('Set-Cookie', []):
                    for scoped in scope_cookie(cookie, prefix):
                        outgoing.add('Set-Cookie', scoped)
                content_type = upstream.headers.get('Content-Type', '')
                vendor_asset = tail.startswith(('static/novnc/', 'static/novnc-login-v2/'))
                if not vendor_asset and any(kind in content_type for kind in ('text/html', 'javascript', 'text/css')):
                    outgoing.popall('ETag', None)
                    outgoing['Cache-Control'] = 'no-store'
                    text = rewrite_text(await upstream.text(), prefix)
                    return web.Response(body=text.encode(), status=upstream.status, headers=outgoing)
                response = web.StreamResponse(status=upstream.status, headers=outgoing)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except (OSError, asyncio.TimeoutError):
            return web.Response(text='<meta http-equiv="refresh" content="5"><p>Account is starting. This view will retry shortly.</p>',
                                content_type='text/html', status=503)

    async def websocket(self, request, url, headers):
        for key in list(headers):
            if key.lower().startswith('sec-websocket-'):
                del headers[key]
        protocols = [p.strip() for p in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if p.strip()]
        async with self.client.ws_connect(url, headers=headers, protocols=protocols) as upstream:
            downstream = web.WebSocketResponse(protocols=protocols, max_msg_size=16 * 1024 * 1024)
            await downstream.prepare(request)

            async def relay(source, target):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await target.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await target.send_bytes(message.data)
                    elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            tasks = [asyncio.create_task(relay(downstream, upstream)), asyncio.create_task(relay(upstream, downstream))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()
            return downstream


def make_app(manager):
    @web.middleware
    async def security(request, handler):
        # IP addresses and local/server names are permitted, arbitrary public DNS
        # names require an explicit allowlist to prevent DNS rebinding.
        hostname = urlsplit('http://' + request.host).hostname or ''
        import ipaddress
        try:
            ipaddress.ip_address(hostname)
            trusted = True
        except ValueError:
            trusted = '.' not in hostname or hostname.endswith(('.local', '.localhost', '.internal', '.home.arpa'))
        trusted = (trusted or hostname in os.environ.get('ALLOWED_HOSTS', '').split(',')
                   or hostname == urlsplit(manager.public_origin).hostname)
        if not trusted:
            raise web.HTTPForbidden(text='Host not allowed; configure ALLOWED_HOSTS')
        native = (request.method, request.path) in NATIVE_ROUTES
        expected_origin = manager.public_origin or f'{request.scheme}://{request.host}'
        if native:
            if (request.headers.get('X-TDM-Request') != '1'
                    or request.headers.get('Sec-Fetch-Site') == 'cross-site'
                    or request.headers.get('Origin') not in (None, expected_origin)):
                raise web.HTTPForbidden()
        elif request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if (request.headers.get('X-Manager-Request') != '1' and not request.path.startswith('/accounts/')):
                raise web.HTTPForbidden()
            if request.headers.get('Origin') != expected_origin:
                raise web.HTTPForbidden(text='Origin mismatch')
        public = {'/_manager/login', '/_manager/login.html', '/_manager/login.js',
                  '/_manager/setup', '/_manager/setup.html', '/_manager/setup.js', '/_manager/status',
                  '/_manager/style.css', '/_manager/health'}
        public.update({'/_manager/icon.svg', '/_manager/icon.png', '/favicon.ico'})
        if not native and request.path not in public and not manager.authenticated(request):
            if request.path.startswith('/_manager/api') or request.path == '/_manager/password':
                raise web.HTTPUnauthorized()
            raise web.HTTPFound('/_manager/login.html' if manager.configured() else '/_manager/setup.html')
        response = await manager.helper.intercept(request, handler)
        if not response.prepared:
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Referrer-Policy'] = 'same-origin'
            response.headers['Cache-Control'] = 'no-store'
            if not request.path.startswith('/accounts/'):
                response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
        return response

    app = web.Application(middlewares=[security], client_max_size=4 * 1024 * 1024)

    async def health(request):
        return web.json_response({'ok': not manager.closing, 'encrypted_storage': not manager.storage_error},
                                 status=503 if manager.storage_error or manager.closing else 200)

    async def file(request):
        name = request.match_info['file']
        if name not in {'index.html', 'login.html', 'setup.html', 'app.js', 'login.js', 'setup.js', 'style.css', 'icon.svg', 'icon.png', 'notifications.js'}:
            raise web.HTTPNotFound()
        return web.FileResponse(UI / name)

    async def home(request):
        return web.FileResponse(UI / 'index.html')

    async def favicon(request):
        return web.FileResponse(UI / 'icon.png')

    async def logout(request):
        await manager.helper.revoke(owner=request.cookies.get(COOKIE, ''))
        manager.sessions.pop(request.cookies.get(COOKIE, ''), None)
        response = web.json_response({'ok': True})
        response.del_cookie(COOKIE, path='/')
        return response

    app.router.add_get('/_manager/health', health)
    app.router.add_post('/_manager/login', manager.login)
    async def status(request):
        return web.json_response({'configured': manager.configured()})
    app.router.add_get('/_manager/status', status)
    app.router.add_post('/_manager/setup', manager.set_password)
    app.router.add_post('/_manager/password', manager.set_password)
    app.router.add_post('/_manager/logout', logout)
    app.router.add_post('/api/helper/connect', manager.helper.native)
    app.router.add_post('/api/helper/session', manager.helper.native)
    app.router.add_get('/api/helper/result', manager.helper.native)
    app.router.add_get('/_manager/api/accounts', manager.api)
    app.router.add_get('/_manager/api/settings', manager.settings)
    app.router.add_post('/_manager/api/settings', manager.settings)
    app.router.add_get('/_manager/api/notifications', manager.notification_settings)
    app.router.add_post('/_manager/api/notifications', manager.notification_settings)
    app.router.add_post('/_manager/api/notifications/test', manager.notification_test)
    app.router.add_post('/_manager/api/accounts', manager.api)
    app.router.add_patch('/_manager/api/accounts/{account}', manager.api)
    app.router.add_delete('/_manager/api/accounts/{account}', manager.delete_account)
    app.router.add_get('/_manager/{file}', file)
    app.router.add_get('/', home)
    app.router.add_get('/favicon.ico', favicon)
    app.router.add_route('*', '/accounts/{account}/{tail:.*}', manager.proxy)
    app.on_startup.append(manager.start)
    app.on_cleanup.append(manager.close)
    return app


if __name__ == '__main__':
    web.run_app(make_app(Manager()), host='0.0.0.0', port=8080, access_log=None, shutdown_timeout=10)
