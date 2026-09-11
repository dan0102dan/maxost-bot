#!/usr/bin/env python3
"""Generate local configuration without displaying secrets."""
import base64
import getpass
import os
import re
import secrets
from pathlib import Path

path = Path(__file__).resolve().parents[1] / '.env'
if path.exists():
    raise SystemExit('.env already exists; refusing to overwrite encryption keys.')

token = getpass.getpass('Telegram bot token (hidden): ').strip()
if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
    raise SystemExit('Invalid token format.')

allowed = input(
    'Allowed Telegram IDs (comma-separated; empty = everyone): '
).strip()
if allowed and not re.fullmatch(r'[0-9]+(?:,[0-9]+)*', allowed):
    raise SystemExit(
        'Use positive numeric Telegram IDs separated by commas, without spaces.'
    )

text = (
    f'TELEGRAM_BOT_TOKEN={token}\n'
    f'POSTGRES_PASSWORD={secrets.token_urlsafe(32)}\n'
    f'ENCRYPTION_KEYS={base64.urlsafe_b64encode(os.urandom(32)).decode()}\n'
    f'ALLOWED_TELEGRAM_IDS={allowed}\n'
    'MAX_ACCOUNTS=100\n'
    'WORKERS=4\n'
)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as file:
    file.write(text)

print('Created .env (mode 0600). Back it up securely.')
print('Enable Topics in BotFather, then run: docker compose up -d --build')
