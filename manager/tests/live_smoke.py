"""Run only against the disposable smoke server; no real Twitch credentials."""
import asyncio
import re
from pathlib import Path

from aiohttp import ClientSession, CookieJar, ClientTimeout


async def main():
    origin = 'http://127.0.0.1:8080'
    headers = {'Origin': origin, 'X-Manager-Request': '1', 'X-TDM-Request': '1'}
    async with ClientSession(cookie_jar=CookieJar(unsafe=True), timeout=ClientTimeout(total=120)) as client:
        async with client.post(origin + '/_manager/login', headers=headers,
                               json={'password': 'synthetic-smoke-dashboard-password'}) as response:
            assert response.status == 200, response.status
        accounts = []
        for name in ('Smoke account A', 'Smoke account B'):
            async with client.post(origin + '/_manager/api/accounts', headers=headers, json={'name': name}) as response:
                assert response.status == 201, await response.text()
                accounts.append((await response.json())['id'])
        for attempt in range(90):
            async with client.get(origin + '/_manager/api/accounts') as response:
                statuses = (await response.json())['accounts']
            if all(a['status'] == 'running' for a in statuses):
                break
            await asyncio.sleep(2)
        else:
            raise AssertionError('Real miner workers did not start')
        for index, account in enumerate(accounts):
            prefix = '/accounts/' + account
            async with client.get(origin + prefix + '/') as response:
                html = await response.text()
                assert response.status == 200, response.status
                assert prefix + '/static/' in html
                assert "frame-ancestors 'self'" in response.headers.get('Content-Security-Policy', '')
            async with client.get(origin + prefix + '/static/app.js') as response:
                javascript = await response.text()
                assert response.status == 200
                assert prefix + '/socket.io' in javascript
                assert "fetch('/api/" not in javascript
            async with client.post(origin + prefix + '/api/settings', headers=headers,
                                   json={'games_to_watch': [f'Synthetic Game {index}']}) as response:
                assert response.status == 200, await response.text()
            async with client.get(origin + prefix + '/socket.io/?EIO=4&transport=polling') as response:
                assert response.status == 200, await response.text()
                assert (await response.text()).startswith('0{')
            async with client.ws_connect(origin + prefix + '/socket.io/?EIO=4&transport=websocket',
                                         headers={'Origin': origin}) as socket:
                packet = await asyncio.wait_for(socket.receive(), 10)
                assert packet.data.startswith('0{')
        for index, account in enumerate(accounts):
            async with client.get(origin + '/accounts/' + account + '/api/settings') as response:
                settings = await response.json()
                assert settings.get('games_to_watch') == [f'Synthetic Game {index}'], list(settings)
        for account in accounts:
            async with client.patch(origin + '/_manager/api/accounts/' + account, headers=headers,
                                    json={'enabled': False}) as response:
                assert response.status == 200, await response.text()
        persisted = list(Path('/data').iterdir())
        assert all(p.suffix == '.enc' for p in persisted), [p.name for p in persisted]
        assert len(persisted) == 3
        assert not any(b'Synthetic Game' in file.read_bytes() for file in persisted)
        print('PASS: two real workers, isolated game settings, account HTML/JS, HTTP polling, WebSockets, encrypted-only persistence.')


asyncio.run(main())
