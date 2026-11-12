#!/usr/bin/env python3
"""Generate a local break-glass console secret; never print it or commit it."""
import os
from pathlib import Path
import secrets
import subprocess
root = Path(__file__).resolve().parent.parent / '.local'
root.mkdir(mode=0o700, exist_ok=True)
os.chmod(root, 0o700)
password = root / 'console-password'
hashed = root / 'console.sha512'
if not hashed.exists():
    os.umask(0o077)
    if not password.exists():
        password.write_text(secrets.token_urlsafe(24) + '\n')
    result = subprocess.run(['openssl', 'passwd', '-6', '-stdin'],
                            input=password.read_text(), text=True, capture_output=True, check=True)
    hashed.write_text(result.stdout)
