"""Run once with a host secret directory mounted at /output."""
import os
from pathlib import Path
from cryptography.fernet import Fernet

os.umask(0o077)
root = Path('/output')
root.mkdir(parents=True, exist_ok=True, mode=0o700)
for name, value in [('account-key', Fernet.generate_key().decode())]:
    path = root / name
    if not path.exists():
        with path.open('x') as stream:
            stream.write(value + '\n')
        path.chmod(0o600)
print('Encryption key is ready. Existing keys were preserved. Create the dashboard password in the WebUI.')
