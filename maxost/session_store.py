"""PyMax StoreProtocol implementation: no plaintext SQLite session files."""
from __future__ import annotations

import asyncio

from .errors import Rejected


class PostgresSessionStore:
    def __init__(self, db, account_id, owner):
        self.db, self.account_id, self.owner = db, account_id, owner
        self.lock = asyncio.Lock()
        self.purpose = f'session:{account_id}'

    async def save_session(self, session_info):
        envelope = self.db.vault.seal(self.owner, self.purpose, session_info.model_dump(mode='json'))
        result = await self.db.pool.execute(
            "UPDATE accounts SET session_cipher=$3 WHERE id=$1 AND owner=$2 AND status NOT IN ('reauth','paused')",
            self.account_id, self.owner, envelope)
        if result != 'UPDATE 1':
            raise Rejected('Сессия отключена владельцем.')

    async def load_session(self):
        from pymax.session import SessionInfo
        blob = await self.db.pool.fetchval(
            'SELECT session_cipher FROM accounts WHERE id=$1 AND owner=$2', self.account_id, self.owner)
        if blob is None:
            return None
        return SessionInfo.model_validate(self.db.vault.open(self.owner, self.purpose, bytes(blob)))

    async def load_session_by_device_id(self, device_id):
        session = await self.load_session()
        return session if session and session.device_id == device_id else None

    async def load_session_by_phone(self, phone):
        session = await self.load_session()
        return session if session and session.phone == phone else None

    async def update_token(self, old_token, new_token, /):
        async with self.lock:
            session = await self.load_session()
            if session and session.token == old_token:
                await self.save_session(session.model_copy(update={'token':new_token}))

    async def delete_session(self, token, /):
        async with self.lock:
            session = await self.load_session()
            if session and session.token == token:
                await self.db.pool.execute('UPDATE accounts SET session_cipher=NULL WHERE id=$1 AND owner=$2', self.account_id, self.owner)

    async def close(self):
        # The application, not an individual MAX runtime, owns the shared pool.
        pass
