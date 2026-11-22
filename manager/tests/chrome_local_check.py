"""Verify genuine Chrome in real login/capture/renewal paths, entirely offline."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, '/app')
os.environ['TDM_DATA_DIR'] = '/runtime/chrome-check/data'
os.environ['TDM_BROWSER_COMPAT'] = 'chrome-brand'  # Stale round-one setting must have no effect.
Path('/runtime/chrome-check').mkdir(parents=True, exist_ok=True)
os.chdir('/runtime/chrome-check')

from aiohttp import ClientSession, web
from src.auth.container_login import ContainerDesktop, LoginChromium
from src.auth.server_renewal import OwnedChromium
from src.auth.session_helper import BrowserExporter, DevToolsConnection


async def main():
    executable = shutil.which('google-chrome-stable')
    assert executable and not shutil.which('chromium')
    assert not Path('/app/src/auth/browser_compat.py').exists()
    assert 'apply_identity' not in Path('/app/src/auth/session_helper.py').read_text()
    assert '--disable-blink-features=AutomationControlled' not in Path('/app/src/auth/server_renewal.py').read_text()
    print(subprocess.check_output([executable, '--version'], text=True).strip(), flush=True)
    print(subprocess.check_output(['dpkg-query', '-W', '-f=${Package} ${Version}', 'google-chrome-stable'], text=True), flush=True)
    received = []

    async def page(request):
        received.append(dict(request.headers))
        response = web.Response(text='<html><title>Local Chrome check</title></html>', content_type='text/html')
        response.set_cookie('round2-persistence-check', 'synthetic-value', max_age=3600, httponly=True)
        return response

    app = web.Application()
    app.router.add_get('/', page)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18771).start()
    url = 'http://127.0.0.1:18771/'

    async def inspect(protocol, navigate=True):
        if navigate:
            await protocol.command('Page.navigate', {'url': url})
        for _ in range(150):
            result = await protocol.command('Runtime.evaluate', {
                'expression': "JSON.stringify({title:document.title,ua:navigator.userAgent,brands:navigator.userAgentData?.brands,platform:navigator.platform})",
                'returnByValue': True})
            identity = json.loads(result['result']['value'])
            if identity.get('title') == 'Local Chrome check' and identity.get('brands'):
                break
            await asyncio.sleep(.1)
        assert any(b['brand'] == 'Google Chrome' for b in identity.get('brands', [])), identity
        assert 'Google Chrome' in received[-1].get('Sec-CH-UA', ''), received[-1]
        assert 'Chrome/' in identity['ua'] and 'Linux' in identity['platform']

    try:
        # Exercise the same resolver used by the real session controller.
        import src.auth.session_controller as controller_module
        import inspect as source_inspect
        assert 'google-chrome-stable' in source_inspect.getsource(controller_module.SessionController.__init__)
        async with OwnedChromium(executable, no_sandbox=True).start() as browser:
            async with browser.target() as protocol:
                await inspect(protocol)
        print('PASS: genuine Chrome renewal pipe and native HTTP/JavaScript branding.', flush=True)

        original_launch = LoginChromium.launch

        async def local_launch(browser, *, capture=False):
            # Redirect only the test fixture's first-party navigation to localhost.
            create = asyncio.create_subprocess_exec
            async def local_process(*args, **kwargs):
                args = [url if item == 'https://www.twitch.tv/login' else item for item in args]
                return await create(*args, **kwargs)
            with patch('asyncio.create_subprocess_exec', local_process):
                return await original_launch(browser, capture=capture)

        with patch.object(LoginChromium, 'launch', local_launch):
            async with ContainerDesktop() as desktop:
                count_before_login = len(received)
                async with LoginChromium(desktop.environment) as browser:
                    assert browser.executable == executable
                    args = Path(f'/proc/{browser.process.pid}/cmdline').read_bytes().split(b'\0')
                    assert not any(b'--load-extension' in arg for arg in args)
                    assert b'--disable-extensions' in args
                    # /proc/PID/exe may be unreadable across the browser UID under
                    # Docker's ptrace policy; verify its actual argv and package instead.
                    assert Path(os.fsdecode(args[0])).name in {'chrome', 'google-chrome', 'google-chrome-stable'}, args[0]
                    for _ in range(100):
                        if len(received) > count_before_login:
                            break
                        await asyncio.sleep(.1)
                    assert len(received) > count_before_login, 'Initial browser did not load the local page'
                    await browser.finish()
                    assert await asyncio.wait_for(browser.process.wait(), 15) == 0
                    print('PASS: genuine Chrome isolated initial browser and manual Finish sign in close.', flush=True)
                    await browser.launch(capture=True)
                    async with BrowserExporter(browser.address).target() as protocol:
                        cookies = await protocol.command('Network.getCookies', {'urls': [url]})
                        assert any(c.get('name') == 'round2-persistence-check' and c.get('value') == 'synthetic-value'
                                   for c in cookies.get('cookies', [])), 'Normal close did not preserve the persistent cookie'
                        await inspect(protocol)
                    print('PASS: genuine Chrome capture, persistent cookie after close/reopen, and native HTTP/JavaScript branding.', flush=True)
    finally:
        await runner.cleanup()


asyncio.run(main())
