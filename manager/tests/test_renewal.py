"""Exercise the patched real controller with synthetic issuance and validation."""
import asyncio
import os
import time
import json
from pathlib import Path
import tempfile
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest

os.environ.setdefault('TDM_DATA_DIR', '/tmp/renewal-unit-data')
sys.path.insert(0, '/app')

from src.auth.session_bundle import SessionError
from src.auth.session_controller import SessionController
from src.web.session_api import SessionAPI
from fastapi import HTTPException
from fastapi import FastAPI
from src.web.auth import WebAuth, AuthMiddleware


class Session:
    def __init__(self, account):
        self.account, self.generation, self.expires = account, 1, 9999999999
        self.available = True
        self.fail = False

    def seed(self):
        return SimpleNamespace(cookie=SimpleNamespace(expires_at=9999999999), bundle=SimpleNamespace(expires_at=self.expires)) if self.available else None

    def status(self):
        return {'user_id': self.account, 'generation': self.generation, 'expires_at': self.expires}

    async def install(self, bundle, *, sdk_cookie, expected_user_id, authorized):
        assert expected_user_id == self.account and authorized()
        if self.fail:
            raise SessionError('CATALOG', stage='renewal_validation', reason='integrity')
        self.generation += 1
        self.expires = max(self.expires, time.time()) + 1000
        return self.status()


class Issuer:
    def __init__(self):
        self.calls = 0
        self.gate = asyncio.Event()
        self.gate.set()

    async def issue(self, seed):
        self.calls += 1
        await self.gate.wait()
        return SimpleNamespace(bundle=SimpleNamespace(to_dict=lambda: {}), cookie=object())


@asynccontextmanager
async def activation():
    yield


class RenewalTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, account=1):
        issuer = Issuer()
        return SessionController(Session(account), issuer=issuer, activation=activation), issuer

    async def wait_for(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail('Renewal did not finish')

    async def test_manual_uses_real_background_loop_and_isolates_accounts(self):
        first, issuer = self.controller(1)
        second, other = self.controller(2)
        issuer.gate.clear()
        first.start()
        second.start()
        try:
            first.request_manual_renewal()
            self.assertTrue(first.status()['renewal']['queued'])
            await self.wait_for(lambda: first.status()['renewal']['running'])
            with self.assertRaises(SessionError) as busy:
                first.request_manual_renewal()
            self.assertEqual(busy.exception.code, 'BUSY')
            issuer.gate.set()
            await self.wait_for(lambda: first.status()['renewal']['last_success_at'])
            result = first.status()
            self.assertEqual(result['session']['generation'], 2)
            self.assertEqual(result['session']['expires_at'], 10000000999)
            self.assertEqual(result['renewal']['last_success_generation'], 2)
            self.assertFalse(result['renewal']['running'])
            self.assertEqual(second.status()['session']['generation'], 1)
            self.assertEqual(other.calls, 0)
            with self.assertRaises(SessionError):
                first.request_manual_renewal()
        finally:
            await first.stop()
            await second.stop()

    async def test_failed_validation_keeps_generation_and_records_no_success(self):
        controller, issuer = self.controller()
        controller.session.fail = True
        with self.assertRaises(SessionError):
            await controller.renew_once()
        state = controller.status()
        self.assertEqual(state['session']['generation'], 1)
        self.assertEqual(state['renewal']['last_error'], 'CATALOG')
        self.assertIsNone(state['renewal']['last_success_at'])
        self.assertFalse(state['renewal']['running'])
        self.assertNotIn('token', str(state))

    async def test_no_seed_rejects_and_api_does_not_report_completion_on_queue(self):
        controller, _ = self.controller()
        client = SimpleNamespace(session_controller=controller)
        api = SessionAPI(None, lambda: client)
        async def status():
            return controller.status()
        api.status = status
        controller.session.available = False
        with self.assertRaises(HTTPException):
            await api.renew()
        controller.session.available = True
        response = await api.renew()
        self.assertTrue(response['renewal']['queued'])
        self.assertIsNone(response['renewal']['last_success_at'])
        with self.assertRaises(HTTPException) as busy:
            await api.renew()
        self.assertEqual(busy.exception.status_code, 429)

    async def test_scheduled_renewal_updates_same_metrics(self):
        controller, issuer = self.controller()
        controller.session.expires = 1
        controller.start()
        try:
            await self.wait_for(lambda: controller.status()['renewal']['last_success_at'])
            self.assertGreaterEqual(issuer.calls, 1)
        finally:
            await controller.stop()

    async def test_http_control_requires_native_auth_and_safe_origin(self):
        controller, _ = self.controller()
        api = SessionAPI(None, lambda: SimpleNamespace(session_controller=controller))
        async def status():
            return controller.status()
        api.status = status
        with tempfile.TemporaryDirectory() as directory:
            auth = WebAuth(Path(directory) / 'auth.json')
            auth.password_hash = 'enabled-for-test'
            auth.sessions[auth.digest('synthetic-session')] = time.time() + 60
            app = FastAPI()
            app.include_router(api.router)
            protected = AuthMiddleware(app, auth)
            async def request(headers, path='/api/session/renew'):
                messages = []
                async def receive():
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                async def send(message):
                    messages.append(message)
                scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                         'scheme': 'http', 'method': 'POST', 'path': path,
                         'raw_path': path.encode(), 'query_string': b'',
                         'root_path': '', 'server': ('127.0.0.1', 12000), 'client': ('127.0.0.1', 45000),
                         'headers': [(b'host', b'127.0.0.1:12000')] + [(k.encode(),v.encode()) for k,v in headers.items()]}
                await protected(scope, receive, send)
                code = next(m['status'] for m in messages if m['type'] == 'http.response.start')
                body = b''.join(m.get('body',b'') for m in messages if m['type'] == 'http.response.body')
                return code, json.loads(body)
            safe = {'x-tdm-request': '1', 'origin': 'http://127.0.0.1:12000'}
            notification_path = '/api/manager/notifications'
            self.assertEqual((await request(safe, notification_path))[0], 401)
            self.assertEqual((await request({'cookie':'tdm_session=synthetic-session'}, notification_path))[0], 403)
            self.assertEqual((await request({**safe, 'cookie':'tdm_session=synthetic-session'}, notification_path))[0], 422)
            self.assertEqual((await request(safe))[0], 401)
            self.assertEqual((await request({'cookie':'tdm_session=synthetic-session'}))[0], 403)
            self.assertEqual((await request({**safe, 'cookie':'tdm_session=synthetic-session', 'origin':'http://other.invalid'}))[0], 403)
            code, body = await request({**safe, 'cookie':'tdm_session=synthetic-session'})
            self.assertEqual(code, 202)
            self.assertTrue(body['renewal']['queued'])
