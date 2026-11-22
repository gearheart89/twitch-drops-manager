"""Headless visual QA against the disposable synthetic smoke server only."""
import asyncio
import contextlib
import json
from pathlib import Path

from aiohttp import ClientSession


async def main():
    browser = await asyncio.create_subprocess_exec(
        'google-chrome-stable', '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
        '--no-first-run', '--remote-debugging-port=9222', '--window-size=1440,1000',
        '--user-data-dir=/tmp/synthetic-preview', 'http://127.0.0.1:8080/',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with ClientSession() as client:
            for _ in range(100):
                try:
                    async with client.get('http://127.0.0.1:9222/json/list') as response:
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
                        result = json.loads((await socket.receive()).data)
                        if result.get('id') == ident:
                            if 'error' in result:
                                raise RuntimeError(result['error'])
                            return result.get('result', {})
                await command('Page.enable')
                await command('Page.navigate', {'url': 'http://127.0.0.1:8080/_manager/login.html'})
                await asyncio.sleep(2)
                result = await command('Runtime.evaluate', {'expression': '''fetch('/_manager/login', {method:'POST',headers:{'Content-Type':'application/json','X-Manager-Request':'1'},body:JSON.stringify({password:'synthetic-smoke-dashboard-password'})}).then(r=>r.status)''', 'awaitPromise': True, 'returnByValue': True})
                assert result['result']['value'] == 200, result
                await command('Page.navigate', {'url': 'http://127.0.0.1:8080/'})
                await asyncio.sleep(6)
                result = await command('Runtime.evaluate', {'expression': "JSON.stringify({title:document.title, accounts:document.querySelectorAll('.account').length, frame:document.querySelector('iframe')?.contentDocument?.title})", 'returnByValue': True})
                summary = json.loads(result['result']['value'])
                assert summary['accounts'] == 2, summary
                assert summary.get('frame'), summary
                screenshot = await command('Page.captureScreenshot', {'format': 'png'})
                print(screenshot['data'])
    finally:
        browser.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(browser.wait(), 10)
        if browser.returncode is None:
            browser.kill()


asyncio.run(main())
