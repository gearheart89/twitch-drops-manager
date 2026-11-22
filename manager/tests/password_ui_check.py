"""Offline browser check using synthetic WebUI passwords, without Twitch accounts."""
import asyncio
import contextlib
import json
from pathlib import Path
import sys

sys.path.insert(0, '/manager')

from aiohttp import ClientSession, web
from cryptography.fernet import Fernet
from server import Manager, make_app
from vault import Vault


async def main():
    manager = Manager(vault=Vault(Path('/runtime/ui-vault'), Fernet.generate_key()), enforce_tmpfs=False)
    assert not manager.configured()
    runner = web.AppRunner(make_app(manager), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18781).start()
    browser = await asyncio.create_subprocess_exec(
        'google-chrome-stable', '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--no-first-run', '--remote-debugging-port=18782', '--window-size=1400,1000',
        '--user-data-dir=/tmp/password-ui-check', 'http://127.0.0.1:18781/',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with ClientSession() as client:
            for _ in range(100):
                try:
                    async with client.get('http://127.0.0.1:18782/json/list') as response:
                        pages = await response.json()
                    page = next(p for p in pages if p['type'] == 'page')
                    break
                except (OSError, StopIteration):
                    await asyncio.sleep(.2)
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
                    return result.get('result', {}).get('value')

                async def until(expression):
                    for _ in range(150):
                        if await evaluate(expression):
                            return
                        await asyncio.sleep(.1)
                    raise AssertionError('Browser condition timed out: ' + expression)

                async def fill(values):
                    await evaluate('Object.entries(' + json.dumps(values) + ').forEach(([id,value])=>document.getElementById(id).value=value)')

                await until("!!document.getElementById('setup-form')")
                setup = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                first = 'synthetic-first-personal-password'
                second = 'synthetic-second-personal-password'
                await fill({'password': first, 'confirmation': first})
                await evaluate("document.getElementById('create-password').click()")
                await until("!!document.getElementById('accounts') && [...document.querySelectorAll('button')].some(b=>b.textContent==='Change password')")
                await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='Change password').click()")
                await until("!!document.querySelector('dialog[open]')")
                dialog = (await command('Page.captureScreenshot', {'format': 'png'}))['data']
                await fill({'current-password': first, 'new-password': second, 'confirm-new-password': second})
                await evaluate("document.getElementById('save-password').click()")
                await until("!document.querySelector('dialog[open]')")
                assert await manager.matches_password(second)
                await evaluate("document.getElementById('logout').click()")
                await until("!!document.getElementById('login-form')")
                await fill({'password': first})
                await evaluate("document.getElementById('unlock').click()")
                await until("!!document.getElementById('error') && !document.getElementById('error').hidden")
                assert not await manager.matches_password(first)
                await fill({'password': second})
                await evaluate("document.getElementById('unlock').click()")
                await until("!!document.getElementById('accounts')")
                # Password management remains accessible at a narrow viewport.
                await command('Emulation.setDeviceMetricsOverride', {'width': 420, 'height': 900, 'deviceScaleFactor': 1, 'mobile': True})
                assert await evaluate("(()=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent==='Change password');return !!b && b.getBoundingClientRect().height>0})()")
                print(json.dumps({'passed': True, 'setup_png': setup, 'password_dialog_png': dialog}))
    finally:
        browser.terminate()
        try:
            await asyncio.wait_for(browser.wait(), 10)
        except asyncio.TimeoutError:
            browser.kill()
            await browser.wait()
        await runner.cleanup()


asyncio.run(main())
