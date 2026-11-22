"""Disposable real-worker smoke server. Never use for normal deployment."""
import os
from pathlib import Path
from cryptography.fernet import Fernet

root = Path('/run/secrets')
root.chmod(0o700)
(root / 'account_key').write_bytes(Fernet.generate_key())
(root / 'dashboard_password').write_text('synthetic-smoke-dashboard-password')
os.execv('/usr/local/bin/python', ['python', '/manager/server.py'])
