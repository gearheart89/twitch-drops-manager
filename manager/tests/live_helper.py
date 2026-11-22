"""Test actual upstream desktop-helper admission, without a real Twitch login."""
import asyncio
from aiohttp import ClientError, ClientSession, ClientTimeout, CookieJar


async def main():
    base = 'http://127.0.0.1:8080'
    headers = {'Origin': base, 'X-Manager-Request': '1', 'X-TDM-Request': '1'}
    async with ClientSession(cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=120)) as browser, \
            ClientSession(headers={'X-TDM-Request': '1'}, timeout=ClientTimeout(total=120)) as native:
        for _ in range(60):
            try:
                async with browser.get(base + '/_manager/health', timeout=ClientTimeout(total=2)) as response:
                    if response.status == 200:
                        break
            except (ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(1)
        else:
            raise AssertionError('Manager did not start')
        async with browser.post(base + '/_manager/login', headers=headers,
                                json={'password': 'synthetic-smoke-dashboard-password'}) as response:
            assert response.status == 200
        async with native.post(base + '/api/helper/connect', json={}) as response:
            assert response.status == 403
        ids = []
        for name in ('Desktop smoke A', 'Desktop smoke B'):
            async with browser.post(base + '/_manager/api/accounts', headers=headers, json={'name': name}) as response:
                assert response.status == 201
                ids.append((await response.json())['id'])
        for _ in range(90):
            async with browser.get(base + '/_manager/api/accounts') as response:
                statuses = (await response.json())['accounts']
            if all(a['status'] == 'running' for a in statuses):
                break
            await asyncio.sleep(2)
        else:
            raise AssertionError('Workers did not start')
        enable = lambda account: base + '/accounts/' + account + '/api/session/helper/enable'
        cancel = lambda account: base + '/accounts/' + account + '/api/session/helper/cancel'
        async with browser.post(enable(ids[0]), headers=headers, json={}) as response:
            assert response.status == 200, await response.text()
        async with browser.post(enable(ids[1]), headers=headers, json={}) as response:
            assert response.status == 409
        async with native.post(base + '/api/helper/connect', json={}) as response:
            assert response.status == 200, await response.text()
            ticket = (await response.json())['connection']
            assert len(ticket) == 43
        async with native.post(base + '/api/helper/connect', json={}) as response:
            assert response.status >= 400
        async with native.get(base + '/api/helper/result', headers={'Authorization': 'Bearer ' + ticket}) as response:
            assert response.status == 200
            assert await response.json() == {'state': 'pending'}
        async with native.post(base + '/api/helper/session', headers={'Authorization': 'Bearer ' + ticket}, json={}) as response:
            assert response.status >= 400  # Upstream rejects a synthetic/invalid seed.
        async with browser.post(cancel(ids[0]), headers=headers, json={}) as response:
            assert response.status == 200
        async with native.get(base + '/api/helper/result', headers={'Authorization': 'Bearer ' + ticket}) as response:
            assert response.status == 403
        async with browser.post(enable(ids[1]), headers=headers, json={}) as response:
            assert response.status == 200, await response.text()
        async with native.post(base + '/api/helper/connect', json={}) as response:
            assert response.status == 200
            second = (await response.json())['connection']
            assert second != ticket
        async with native.get(base + '/api/helper/result', headers={'Authorization': 'Bearer ' + ticket}) as response:
            assert response.status >= 400
        async with browser.post(base + '/_manager/logout', headers=headers) as response:
            assert response.status == 200
        async with native.get(base + '/api/helper/result', headers={'Authorization': 'Bearer ' + second}) as response:
            assert response.status == 403
        print('PASS: actual upstream helper admission, first-client restriction, bearer receipts, seed validation, account binding, cancellation, and logout revocation.')


asyncio.run(main())
