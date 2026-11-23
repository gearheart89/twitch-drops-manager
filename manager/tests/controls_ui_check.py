"""Real Chrome UI check with two offline account fixtures and no credentials."""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, '/manager')
from aiohttp import ClientSession, web
from cryptography.fernet import Fernet
from server import Manager, make_app
from vault import Vault


async def main():
    manager = Manager(vault=Vault(Path('/runtime/controls-vault'), Fernet.generate_key()),
                      password='synthetic-ui-password', enforce_tmpfs=False)
    async def no_spawn(account):
        pass
    manager.spawn = no_spawn
    runners = []
    states = {}
    tasks = []
    for index, name in enumerate(('Account One', 'Account Two')):
        account_id = ('one', 'two')[index]
        port = 18800 + index
        state = {'logged_in': True, 'renewal_available': True, 'renewal_error': None,
                 'session': {'generation': 1, 'expires_at': time.time() + 3600},
                 'renewal': {'running': False, 'queued': False, 'last_success_at': None}}
        states[account_id] = state
        async def page(request, name=name):
            return web.Response(text='''<!doctype html><html><head><style>body{font-family:system-ui;background:#f6f5fa;color:#242039;padding:32px}body.dark-mode{background:#10121a;color:#eef0fa}p{opacity:.7}</style></head><body><h1>'''+name+'''</h1><p>Offline account fixture · no Twitch connection</p><label><input id="dark-mode" type="checkbox">Account theme</label><script src="/static/account-theme.js"></script></body></html>''', content_type='text/html')
        async def theme_script(request):
            return web.Response(text=Path('/manager/web/account-theme.js').read_text(), content_type='text/javascript')
        async def status(request, state=state):
            return web.json_response(state)
        async def renew(request, state=state):
            assert request.headers.get('X-TDM-Request') == '1'
            state['renewal']['queued'] = True
            async def complete():
                await asyncio.sleep(.3)
                state['renewal'].update(queued=False, running=True)
                await asyncio.sleep(.5)
                state['session']['generation'] += 1
                state['session']['expires_at'] += 3600
                state['renewal'].update(running=False, last_success_at=time.time())
            tasks.append(asyncio.create_task(complete()))
            return web.json_response(state, status=202)
        worker = web.Application()
        worker.router.add_get('/', page)
        worker.router.add_get('/healthz', status)
        worker.router.add_get('/static/account-theme.js', theme_script)
        worker.router.add_get('/api/session', status)
        worker.router.add_post('/api/session/renew', renew)
        runner = web.AppRunner(worker, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', port).start()
        runners.append(runner)
        manager.accounts.append({'id': account_id, 'name': name, 'port': port, 'enabled': True})
    runner = web.AppRunner(make_app(manager), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18781).start()
    runners.append(runner)
    browser = await asyncio.create_subprocess_exec('google-chrome-stable', '--headless', '--no-sandbox',
        '--disable-gpu', '--disable-dev-shm-usage', '--no-first-run', '--remote-debugging-port=18782',
        '--window-size=1440,1000', '--user-data-dir=/tmp/controls-browser', 'http://127.0.0.1:18781/',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with ClientSession() as client:
            for _ in range(300):
                try:
                    async with client.get('http://127.0.0.1:18782/json/list') as response:
                        pages = await response.json()
                    page = next(p for p in pages if p['type'] == 'page')
                    break
                except (OSError, StopIteration):
                    await asyncio.sleep(.2)
            else:
                raise AssertionError('Chrome debugging endpoint did not become ready')
            async with client.ws_connect(page['webSocketDebuggerUrl']) as socket:
                counter = 0
                async def command(method, params=None):
                    nonlocal counter
                    counter += 1
                    ident = counter
                    await socket.send_json({'id': ident, 'method': method, 'params': params or {}})
                    while True:
                        message = json.loads((await socket.receive()).data)
                        if message.get('id') == ident:
                            if 'error' in message:
                                raise RuntimeError(message['error'])
                            return message.get('result', {})
                async def evaluate(expression):
                    result = await command('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
                    if result.get('exceptionDetails'):
                        return None
                    return result.get('result', {}).get('value')
                async def until(expression):
                    for _ in range(180):
                        if await evaluate(expression):
                            return
                        await asyncio.sleep(.1)
                    raise AssertionError('Timed out: ' + expression)
                await until("!!document.getElementById('login-form')")
                await evaluate("document.getElementById('password').value='synthetic-ui-password';document.getElementById('unlock').click()")
                await until("document.getElementById('workspace')?.contentDocument?.body.classList.contains('dark-mode')")
                await until("document.getElementById('renew-now')?.disabled===false")
                dark = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                await evaluate("document.getElementById('theme-toggle').click()")
                await until("document.documentElement.dataset.theme==='light' && !document.getElementById('workspace').contentDocument.body.classList.contains('dark-mode')")
                await evaluate("document.querySelectorAll('.account')[1].click()")
                await until("document.getElementById('workspace').contentDocument?.querySelector('h1')?.textContent==='Account Two'")
                assert not await evaluate("document.getElementById('workspace').contentDocument.body.classList.contains('dark-mode')")
                await command('Page.reload')
                await until("document.documentElement.dataset.theme==='light' && document.querySelectorAll('.account').length===2")
                await evaluate("document.querySelectorAll('.account')[0].click()")
                await until("document.getElementById('renew-now')?.disabled===false")
                await evaluate("document.getElementById('renew-now').click()")
                await until("document.getElementById('renewal-status').textContent.includes('Last successful renewal:') && document.getElementById('renewal-status').textContent.includes('Generation: 2')")
                assert states['one']['session']['generation'] == 2
                assert states['two']['session']['generation'] == 1
                light = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                assert await evaluate("document.querySelector('link[rel=icon]').href.endsWith('/_manager/icon.svg')")
                await command('Emulation.setDeviceMetricsOverride', {'width': 420, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': True})
                assert await evaluate("document.getElementById('theme-toggle').getBoundingClientRect().height>0 && document.getElementById('renew-now').getBoundingClientRect().right<=innerWidth")
                mobile = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                manager.notifications.observe(manager.accounts[0], {
                    'logged_in': True, 'renewal_error': None, 'inventory_ready': True,
                    'observed_at': time.time(), 'refresh_minutes': 30, 'claims': [], 'channels': {},
                    'campaigns': [{'id': 'fixture-campaign', 'game': 'Sample Game', 'campaign': 'Sample Campaign',
                                   'linked': False, 'link_url': 'https://example.com/link'}]})
                await evaluate("window.refreshNotifications()")
                await until("document.getElementById('link-alerts').hidden===false")
                link_mobile = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                await evaluate("document.getElementById('notifications-button').click()")
                await until("document.getElementById('notifications-dialog').open")
                assert not await evaluate("document.getElementById('mqtt-enabled').checked")
                await evaluate("document.getElementById('mqtt-host').value='mqtt.example.invalid';document.getElementById('mqtt-password').value='synthetic-browser-broker-password';document.getElementById('notifications-save').click()")
                await until("!document.getElementById('notifications-dialog').open")
                await evaluate("document.getElementById('notifications-button').click()")
                await until("document.getElementById('notifications-dialog').open")
                assert await evaluate("document.getElementById('mqtt-password').value==='' && document.getElementById('mqtt-password').placeholder.includes('Saved password')")
                notifications_mobile = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                await command('Emulation.clearDeviceMetricsOverride')
                notifications = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                await evaluate("document.getElementById('notifications-cancel').click()")
                link_desktop = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                manager.notifications.observations['one']['alerts'][0]['link_url'] = 'https://example.com:wrong/link'
                await evaluate("window.refreshNotifications()")
                await evaluate("document.getElementById('notifications-button').click()")
                await until("document.getElementById('notifications-dialog').open")
                await evaluate("document.getElementById('notifications-cancel').click()")
                await evaluate("document.getElementById('delete-account').click()")
                await until("!!document.querySelector('dialog[open]')")
                await evaluate("document.getElementById('delete-name').value='wrong name';document.getElementById('confirm-delete').click()")
                await until("!document.getElementById('delete-error').hidden")
                assert len(manager.accounts) == 2
                deletion = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                await evaluate("document.getElementById('delete-name').value='Account One';document.getElementById('confirm-delete').click()")
                await until("!document.querySelector('dialog[open]') && document.querySelectorAll('.account').length===1")
                assert manager.accounts[0]['id'] == 'two'
                print(json.dumps({'passed': True, 'dark_png': dark, 'light_png': light, 'mobile_png': mobile,
                                  'deletion_png': deletion, 'notifications_png': notifications,
                                  'link_desktop_png': link_desktop, 'link_mobile_png': link_mobile,
                                  'notifications_mobile_png': notifications_mobile}))
    finally:
        browser.terminate()
        try:
            await asyncio.wait_for(browser.wait(), 10)
        except asyncio.TimeoutError:
            browser.kill()
            await browser.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runner in reversed(runners):
            await runner.cleanup()


asyncio.run(main())
