"""Asserted integration changes for the pinned miner and genuine Chrome."""
from pathlib import Path


def replace(path, before, after):
    file = Path('/app') / path
    content = file.read_text()
    if content.count(before) != 1:
        raise RuntimeError(f'Unsupported upstream source: {path}')
    file.write_text(content.replace(before, after))


replace('src/config/paths.py', 'DATA_DIR = PROJECT_ROOT / "data"',
        'import os\nDATA_DIR = Path(os.environ["TDM_DATA_DIR"])')
replace('src/__main__.py', 'webapp.run_server(host="0.0.0.0", port=8080)',
        'webapp.run_server(host="127.0.0.1", port=int(__import__("os").environ["TDM_PORT"]))')
replace('src/web/auth.py', "frame-ancestors 'none'", "frame-ancestors 'self'")

# Use the explicit vendor executable in initial login, capture and SDK renewal.
for source in ('src/auth/container_login.py', 'src/auth/session_controller.py'):
    replace(source, 'shutil.which("chromium") or shutil.which("chromium-browser")',
            'shutil.which("google-chrome-stable") or shutil.which("google-chrome")')
replace('src/auth/server_renewal.py', 'default="chromium"', 'default="google-chrome-stable"')
replace('src/auth/session_controller.py', 'or "chromium",', 'or "google-chrome-stable",')

# Stable Google Chrome does not load unpacked extensions via this flag.
# The existing Finish sign in button closes/flushed the owned login profile.
replace('src/auth/container_login.py',
        '            args.append(f"--load-extension={Path(__file__).with_name(\'login_completion\')}")',
        '            args.append("--disable-extensions")')
replace('src/auth/container_login.py',
        '("Xvfb", "openbox", "x11vnc", "xdotool")',
        '("Xvfb", "openbox", "x11vnc", "xdotool", "wmctrl")')
replace('src/auth/container_login.py',
        '        process = await asyncio.create_subprocess_exec("xdotool", *args, env=self.environment,',
        '        executable = "xdotool"\n'
        '        if args and args[0] == "windowquit":\n'
        '            executable = "wmctrl"\n'
        '            args = ("-i", "-c", hex(int(args[1])))\n'
        '        process = await asyncio.create_subprocess_exec(executable, *args, env=self.environment,')

# Retire the previous experiment, including stale Unraid environment settings.
# No user-agent, client-hint, platform or automation-feature overrides are added.
replace('src/auth/server_renewal.py',
        '                "--disable-blink-features=AutomationControlled", "about:blank",',
        '                "about:blank",')

# Queue manual renewals through the existing background controller. The caller
# never receives proof material, and no second process edits live credentials.
extension = Path('/manager/renewal_extension.py').read_text()
(Path('/app/src/auth') / 'manager_renewal.py').write_text(extension)
controller = Path('/app/src/auth/session_controller.py')
with controller.open('a') as stream:
    stream.write('\n\nfrom src.auth.manager_renewal import install as _install_manager_renewal\n'
                 '_install_manager_renewal(SessionController)\n')
replace('src/web/session_api.py',
        '        self.router.add_api_route("/api/session", self.status, methods=["GET"])',
        '        self.router.add_api_route("/api/session", self.status, methods=["GET"])\n'
        '        self.router.add_api_route("/api/session/renew", self.renew, methods=["POST"], status_code=202)')
replace('src/web/session_api.py', '    async def accept_token(self, request: Request):',
        '    async def renew(self):\n'
        '        try:\n'
        '            self.client().session_controller.request_manual_renewal()\n'
        '            return await self.status()\n'
        '        except SessionError as error:\n'
        '            if error.code == "BUSY":\n'
        '                raise HTTPException(429, "session_renewal_busy") from None\n'
        '            raise self.failure(error) from None\n\n'
        '    async def accept_token(self, request: Request):')

# The manager owns appearance. Account settings and socket broadcasts must not
# overwrite it, including when an account still has a saved light/dark preference.
theme_block = '''    if ({source}.dark_mode) {{
        document.body.classList.add('dark-mode');
    }} else {{
        document.body.classList.remove('dark-mode');
    }}'''
for source in ('data', 'settings'):
    replace('web/static/app.js', theme_block.format(source=source), '    applyManagerTheme();')
replace('web/static/app.js',
        "    document.getElementById('dark-mode').checked = settings.dark_mode || false;",
        "    document.getElementById('dark-mode').disabled = true;\n"
        "    document.getElementById('dark-mode').closest('label').hidden = true;\n"
        "    document.getElementById('dark-mode').closest('label').title = 'Theme is controlled by the account manager';\n"
        "    applyManagerTheme();")
frontend = Path('/app/web/static/app.js')
frontend.write_text(Path('/manager/web/account-theme.js').read_text() + '\n' + frontend.read_text())
Path('/app/web/static/favicon.png').write_bytes(Path('/manager/web/icon.png').read_bytes())
