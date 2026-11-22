"""Verify real embedded sign-in transport against the disposable smoke instance."""
import asyncio
from aiohttp import ClientSession, ClientTimeout, CookieJar, WSMsgType


async def main():
    base = 'http://127.0.0.1:8080'
    headers = {'Origin': base, 'X-Manager-Request': '1'}
    async with ClientSession(cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=90)) as client:
        async with client.post(base + '/_manager/login', headers=headers,
                               json={'password': 'synthetic-smoke-dashboard-password'}) as response:
            assert response.status == 200
        async with client.get(base + '/_manager/api/accounts') as response:
            account = (await response.json())['accounts'][0]['id']
        async with client.patch(base + '/_manager/api/accounts/' + account, headers=headers,
                                json={'enabled': True}) as response:
            assert response.status == 200
        for attempt in range(60):
            async with client.get(base + '/accounts/' + account + '/api/session') as response:
                if response.status == 200:
                    browser = (await response.json())['browser']
                    if browser['state'] == 'sign_in':
                        break
                    if browser.get('error'):
                        raise AssertionError('Browser startup error: ' + browser['error'])
            await asyncio.sleep(2)
        else:
            raise AssertionError('Embedded sign-in browser did not become ready')
        async with client.ws_connect(base + '/accounts/' + account + '/api/session/vnc',
                                     headers={'Origin': base}) as connection:
            message = await asyncio.wait_for(connection.receive(), 10)
            assert message.type == WSMsgType.BINARY and message.data.startswith(b'RFB ')
        print('PASS: embedded Twitch sign-in browser ready; real VNC binary WebSocket handshake forwarded.')


asyncio.run(main())
