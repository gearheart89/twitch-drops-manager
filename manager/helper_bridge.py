"""Route the upstream native helper to one explicitly selected account.

The helper keeps its upstream bearer-authenticated protocol and root dashboard URL.
Only these three routes bypass dashboard cookies, during the owner's login window.
"""
import asyncio
import time

from aiohttp import ClientError, ClientTimeout, web

NATIVE_ROUTES = frozenset({('POST', '/api/helper/connect'),
                           ('POST', '/api/helper/session'),
                           ('GET', '/api/helper/result')})


class HelperBridge:
    def __init__(self, manager):
        self.manager = manager
        self.window = None
        self.lock = asyncio.Lock()

    def live(self):
        window = self.window
        if not window:
            return None
        account = self.manager.account(window['account'])
        now = time.time()
        if (window['until'] <= now or self.manager.sessions.get(window['owner'], 0) <= now
                or not account or not account['enabled']):
            return None
        return window

    async def revoke(self, account_id=None, owner=None, cancel=True):
        window = self.window
        if (not window or (account_id is not None and window['account'] != account_id)
                or (owner is not None and window['owner'] != owner)):
            return
        self.window = None  # Close admission before awaiting the upstream cancellation.
        if not cancel:
            return
        account = self.manager.account(window['account'])
        if account and self.manager.client:
            try:
                async with self.manager.client.post(
                    f"http://127.0.0.1:{account['port']}/api/session/helper/cancel",
                    headers={'X-TDM-Request': '1', 'Cookie': window['cookie']},
                    timeout=ClientTimeout(total=10), allow_redirects=False,
                ) as response:
                    await response.read()
            except (ClientError, OSError, asyncio.TimeoutError):
                pass  # Manager admission stays closed even if the worker has exited.

    async def expire(self):
        if self.window and not self.live():
            await self.revoke()

    async def completed(self, window):
        account = self.manager.account(window['account'])
        if not account:
            return False
        try:
            async with self.manager.client.get(
                f"http://127.0.0.1:{account['port']}/api/session",
                headers={'Cookie': window['cookie']}, timeout=ClientTimeout(total=5),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return False
                data = await response.json()
                return data.get('logged_in') is True and data.get('helper', {}).get('state') == 'complete'
        except (ClientError, OSError, ValueError, asyncio.TimeoutError):
            return False

    async def intercept(self, request, handler):
        account_id = request.match_info.get('account')
        tail = request.match_info.get('tail', '')
        if request.method != 'POST' or not account_id:
            return await handler(request)
        if tail in {'api/session/helper/cancel', 'api/session/logout', 'api/session/token'}:
            await self.revoke(account_id=account_id, cancel=False)
        if tail != 'api/session/helper/enable':
            return await handler(request)
        async with self.lock:
            await self.expire()
            current = self.live()
            owner = request.cookies.get('drops_manager', '')
            if current and (current['account'] != account_id or current['owner'] != owner):
                if not await self.completed(current):
                    return web.json_response({'detail': 'Finish or cancel the other account desktop login first'}, status=409)
            if current:
                await self.revoke()
            # Check manager authentication again after any awaited cancellation.
            if not self.manager.authenticated(request):
                raise web.HTTPUnauthorized()
            response = await handler(request)
            if response.status == 200 and self.manager.authenticated(request):
                self.window = {
                    'account': account_id, 'owner': owner,
                    'until': min(time.time() + 600, self.manager.sessions[owner]),
                    'cookie': '; '.join(f'{k}={v}' for k, v in request.cookies.items() if k != 'drops_manager'),
                }
            return response

    async def native(self, request):
        window = self.live()
        if not window:
            return web.json_response({'detail': 'session_helper_disabled'}, status=403)
        if len(await request.read()) > 65536:
            raise web.HTTPRequestEntityTooLarge(max_size=65536, actual_size=request.content_length or 65537)
        # Capture the account now; changing the selected UI tab never changes routing.
        return await self.manager.proxy(request, account_id=window['account'], tail=request.path.lstrip('/'))
