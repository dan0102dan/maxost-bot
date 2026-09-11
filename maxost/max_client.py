"""Personal MAX account lifecycle. All version-sensitive calls live in this adapter."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from .errors import Rejected, RetryLater, Uncertain
from .session_store import PostgresSessionStore

log = logging.getLogger(__name__)


class NoInteractiveLogin:
    async def authenticate(self, app):
        # Never request another SMS automatically on a server restart.
        raise Rejected('Требуется повторный вход: /disconnect, затем /connect.')


@dataclass
class Connection:
    account: object
    client: object = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task | None = None
    closing: bool = False


class MaxHub:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.connections = {}
        self.on_event = None
        self.on_history = None

    def build(self, entry, provider=None):
        from pymax import Client, ExtraConfig
        a = entry.account
        phone = self.db.vault.open(a['owner'], f"phone:{a['id']}", bytes(a['phone_cipher']))
        client = Client(
            phone=phone, work_dir='/tmp',
            sms_code_provider=provider, password_provider=provider,
            auth_flow=None if provider else NoInteractiveLogin(),
            extra_config=ExtraConfig(
                store=PostgresSessionStore(self.db, a['id'], a['owner']),
                persist_session=True, reconnect=False, relogin=False,
                password_max_attempts=3, log_level='CRITICAL',
            ),
        )
        # Upstream logging may contain protocol payloads on failures. Disable its
        # entire hierarchy, including loggers created by future Client instances.
        for name, logger in logging.Logger.manager.loggerDict.items():
            if name.startswith('pymax') and isinstance(logger, logging.Logger):
                logger.disabled = True

        async def dispatch(kind, event):
            if entry.ready.is_set() and not entry.closing and self.on_event:
                try:
                    await self.on_event(entry, kind, event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # No exception text, repr(event), phone, token or raw update.
                    log.warning('MAX event processing failed (%s); history recovery will retry new messages', type(exc).__name__)

        @client.on_message()
        async def message(event, _client):
            await dispatch('send', event)

        @client.on_message_edit()
        async def edit(event, _client):
            await dispatch('edit', event)

        @client.on_message_delete()
        async def delete(event, _client):
            await dispatch('delete', event)

        return client

    async def authenticate(self, account, provider):
        entry = Connection(account)
        self.connections[account['id']] = entry
        entry.client = self.build(entry, provider)
        try:
            await entry.client.connect()
            await self.identify(entry)
        except BaseException:
            # A successfully issued but unbound session must not remain running.
            with contextlib.suppress(Exception):
                if entry.client.is_connected:
                    await entry.client.logout()
            with contextlib.suppress(Exception):
                await entry.client.close()
            self.connections.pop(account['id'], None)
            raise
        entry.task = asyncio.create_task(self.monitor(entry, connected=True))
        return entry

    async def identify(self, entry):
        me = entry.client.me
        max_id = int(me.contact.id) if me and me.contact else None
        if max_id is None:
            raise Rejected('MAX не вернул профиль аккаунта.')
        previous = entry.account['max_user_id']
        if previous is not None and previous != max_id:
            raise Rejected('Идентификатор сессии изменился. Подключение остановлено.')
        await self.db.activate(entry.account['id'], entry.account['owner'], max_id)
        entry.account = await self.db.account(entry.account['owner'])
        entry.ready.set()

    async def restore(self):
        for account in await self.db.pool.fetch("SELECT * FROM accounts WHERE status IN ('offline','connected')"):
            entry = Connection(account)
            self.connections[account['id']] = entry
            entry.task = asyncio.create_task(self.monitor(entry))

    async def monitor(self, entry, connected=False):
        delay = 2
        try:
            while not entry.closing:
                try:
                    if not connected:
                        entry.client = self.build(entry)
                        await entry.client.connect()
                        await self.identify(entry)
                    delay, connected = 2, False
                    # A periodic history pass also recovers a failed live-event DB write.
                    while entry.client.is_connected and not entry.closing:
                        if self.on_history:
                            try:
                                async with entry.gate:
                                    await self.on_history(entry)
                            except Exception as exc:
                                log.warning('MAX history recovery deferred (%s)', type(exc).__name__)
                        for _ in range(self.settings.history_interval):
                            await asyncio.sleep(1)
                            if not entry.client.is_connected or entry.closing:
                                break
                    raise ConnectionError('connection closed')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    entry.ready.clear()
                    with contextlib.suppress(Exception):
                        if entry.client:
                            await entry.client.close()
                    # API rejection while connecting is not a network retry or an
                    # invitation to request more SMS. The owner must reconnect.
                    terminal = isinstance(exc, Rejected) or type(exc).__name__ in ('ApiError','PasswordAttemptsExceededError')
                    status = 'reauth' if terminal else 'offline'
                    await self.db.pool.execute('UPDATE accounts SET status=$3 WHERE id=$1 AND owner=$2', entry.account['id'], entry.account['owner'], status)
                    log.warning('MAX connection %s (%s)', status, type(exc).__name__)
                    if terminal:
                        return
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
        finally:
            entry.ready.clear()
            with contextlib.suppress(Exception):
                if entry.client:
                    await entry.client.close()

    def get(self, account_id, owner):
        entry = self.connections.get(account_id)
        if not entry or entry.account['owner'] != owner or entry.closing or not entry.ready.is_set():
            raise RetryLater('MAX сейчас недоступен; сообщение осталось в очереди.', 10)
        return entry

    async def disconnect(self, owner):
        account = await self.db.account(owner)
        if not account:
            return True
        entry = self.connections.get(account['id'])
        await self.db.pool.execute("UPDATE accounts SET status='paused' WHERE id=$1 AND owner=$2", account['id'], owner)
        revoked = False
        if entry:
            entry.closing = True
            # Let a started delivery finish before revoking and deleting mappings.
            async with entry.gate:
                try:
                    if entry.client and entry.client.is_connected:
                        await entry.client.logout()
                        revoked = True
                except Exception:
                    pass
            if entry.task:
                entry.task.cancel()
                await asyncio.gather(entry.task, return_exceptions=True)
            if entry.client:
                with contextlib.suppress(Exception):
                    await entry.client.close()
            self.connections.pop(account['id'], None)
        await self.db.pool.execute('DELETE FROM accounts WHERE id=$1 AND owner=$2', account['id'], owner)
        await self.db.pool.execute('DELETE FROM users WHERE id=$1', owner)
        return revoked

    async def close(self):
        entries = list(self.connections.values())
        for entry in entries:
            entry.closing = True
            if entry.task:
                entry.task.cancel()
        await asyncio.gather(*(e.task for e in entries if e.task), return_exceptions=True)
        for entry in entries:
            if entry.client:
                with contextlib.suppress(Exception):
                    await entry.client.close()


async def mutate(client, method, **kwargs):
    """Do not guess whether a timed-out MAX mutation reached the server."""
    try:
        return await getattr(client, method)(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if type(exc).__name__ == 'ApiError':
            raise Rejected('MAX отклонил действие. Проверьте доступность диалога и /status.') from None
        raise Uncertain('Ответ MAX не получен. Проверьте переписку перед повторной отправкой.') from None
