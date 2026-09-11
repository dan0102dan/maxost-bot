from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from pathlib import Path

from .auth import Authentication
from .bot_text import HELP, status_text
from .bridge import Bridge
from .config import Settings
from .content import is_private_update
from .crypto import Vault
from .db import Database
from .errors import BridgeError, Rejected, RetryLater
from .max_client import MaxHub
from .media import Media
from .telegram import Telegram

log = logging.getLogger(__name__)


class Application:
    def __init__(self, settings, db, tg):
        self.settings, self.db, self.tg = settings, db, tg
        self.hub = MaxHub(db, settings)
        self.media = Media(tg, settings.max_file_bytes)
        self.bridge = Bridge(db, tg, self.hub, self.media, settings)
        self.auth = Authentication(db, tg, self.hub, settings)

    def allowed(self, owner):
        return (
            isinstance(owner, int)
            and owner > 0
            and (
                not self.settings.allowed_users
                or owner in self.settings.allowed_users
            )
        )

    async def status(self, owner, thread=None):
        account = await self.db.account(owner)
        if not account:
            await self.tg.text(owner, 'MAX не подключён. /connect', thread)
            return
        rows = await self.db.pool.fetch(
            'SELECT status,count(*) AS n FROM jobs WHERE owner=$1 GROUP BY status',
            owner,
        )
        counts = {row['status']: row['n'] for row in rows}
        failed = []
        if counts.get('failed') or counts.get('unknown'):
            failed = await self.db.pool.fetch(
                "SELECT id,status FROM jobs WHERE owner=$1 "
                "AND status IN ('failed','unknown') ORDER BY id LIMIT 10",
                owner,
            )
        await self.tg.text(
            owner, status_text(account['status'], counts, failed), thread
        )

    async def command(self, owner, text, message=None):
        args = text.split()
        command = args[0].split('@', 1)[0].lower()
        thread = message.get('message_thread_id') if message else None

        if command in ('/start', '/connect'):
            if await self.db.account(owner):
                await self.status(owner, thread)
            else:
                await self.auth.start(owner)
        elif command == '/cancel':
            await self.auth.cancel(owner)
        elif command == '/disconnect':
            await self.auth.start(owner, disconnect=True)
        elif command == '/status':
            await self.status(owner, thread)
        elif command == '/help':
            await self.tg.text(owner, HELP, thread)
        elif command in ('/retry', '/skip'):
            try:
                job_id = int(args[1])
                if not 0 < job_id < 2**63:
                    raise ValueError()
            except (ValueError, IndexError):
                raise Rejected(
                    'Укажите номер: /retry 123 или /skip 123.'
                ) from None
            await self.db.queue_control(
                owner,
                job_id,
                command[1:],
                len(args) > 2 and args[2] == 'confirm',
            )
            await self.tg.text(
                owner,
                'Повторяем отправку.' if command == '/retry' else 'Сообщение пропущено.',
                thread,
            )
        elif command == '/bind':
            if len(args) != 2:
                raise Rejected('Отправьте /bind UUID в нужном топике.')
            await self.bridge.bind(owner, args[1], thread)
            await self.tg.text(
                owner, 'Топик привязан. Повторить отправку: /retry N.', thread
            )
        elif command == '/delete':
            if not message:
                raise Rejected('Отправьте /delete ответом на сообщение.')
            await self.bridge.delete_from_telegram(message)
        else:
            raise Rejected('Неизвестная команда. /help')

    async def handle(self, update):
        if 'callback_query' in update:
            query = update['callback_query']
            owner = query.get('from', {}).get('id')
            message = query.get('message', {})
            if (
                not self.allowed(owner)
                or message.get('chat', {}).get('type') != 'private'
                or message.get('chat', {}).get('id') != owner
            ):
                return
            try:
                data = query.get('data', '')
                if data.startswith('np:'):
                    await self.bridge.interactions.native_polls.callback(query)
                    return
                with contextlib.suppress(BridgeError):
                    await self.tg.call(
                        'answerCallbackQuery',
                        {'callback_query_id': query['id']},
                    )
                if data in ('nav:connect', 'nav:status', 'nav:help'):
                    await self.command(owner, '/' + data.split(':')[1])
                else:
                    await self.auth.callback(query)
            except BridgeError as exc:
                if isinstance(exc, RetryLater):
                    raise
                with contextlib.suppress(BridgeError):
                    await self.tg.call(
                        'answerCallbackQuery',
                        {
                            'callback_query_id': query['id'],
                            'text': str(exc)[:200],
                            'show_alert': True,
                        },
                    )
            return

        if 'poll_answer' in update:
            answer = {**update['poll_answer'], 'update_id': update['update_id']}
            owner = answer.get('user', {}).get('id')
            if self.allowed(owner):
                try:
                    await self.bridge.interactions.native_polls.answer(answer)
                except Rejected as exc:
                    await self.tg.text(owner, str(exc))
            return

        if 'message_reaction' in update:
            reaction = {
                **update['message_reaction'],
                'update_id': update['update_id'],
            }
            owner = reaction.get('user', {}).get('id')
            if self.allowed(owner):
                try:
                    await self.bridge.interactions.native_reaction(reaction)
                except Rejected as exc:
                    await self.tg.text(owner, str(exc))
            return

        if 'my_chat_member' in update:
            change = update['my_chat_member']
            chat = change.get('chat', {})
            if (
                chat.get('type') == 'private'
                and change.get('new_chat_member', {}).get('status') == 'kicked'
            ):
                owner = chat['id']
                await self.auth.cancel(owner, display=False)
                await self.hub.disconnect(owner)
            return

        message = update.get('message') or update.get('edited_message')
        if (
            not message
            or not is_private_update(message)
            or not self.allowed(message['from']['id'])
        ):
            return
        owner = message['from']['id']
        if any(key.startswith('forum_topic_') for key in message):
            return

        try:
            if await self.auth.password(message):
                return
            if message.get('text', '').startswith('/'):
                if 'edited_message' not in update:
                    await self.command(owner, message['text'], message)
                return
            screen = self.auth.screens.get(owner)
            if screen and screen.phase in (
                'consent', 'phone', 'requesting', 'code', 'checking'
            ):
                await self.tg.remove(owner, message['message_id'])
                raise Rejected('Используйте кнопки входа. Отмена: /cancel.')
            await self.bridge.from_telegram(
                message,
                edited='edited_message' in update,
                event_id=update.get('update_id', 0),
            )
        except Rejected as exc:
            await self.tg.text(
                owner, str(exc), message.get('message_thread_id')
            )

    async def poll(self):
        offset = await self.db.get_offset()
        while True:
            try:
                updates = await self.tg.call(
                    'getUpdates',
                    {
                        'offset': offset,
                        'timeout': 30,
                        'limit': 50,
                        'allowed_updates': [
                            'message',
                            'edited_message',
                            'callback_query',
                            'my_chat_member',
                            'message_reaction',
                            'poll_answer',
                        ],
                    },
                )
                for update in updates:
                    try:
                        await self.handle(update)
                    except BridgeError as exc:
                        if isinstance(exc, RetryLater):
                            raise
                        log.warning('Update rejected (%s)', type(exc).__name__)
                    await self.db.set_offset(update['update_id'] + 1)
                    offset = update['update_id'] + 1
                    update.clear()
            except RetryLater as exc:
                await asyncio.sleep(min(max(exc.delay, 1), 60))

    async def maintenance(self, lock_connection):
        last_cleanup = 0
        while True:
            await lock_connection.fetchval('SELECT 1')
            await self.auth.expire()
            await self.bridge.notify_errors()
            if time.monotonic() - last_cleanup > 3600:
                await self.db.cleanup(self.settings.retention_days)
                last_cleanup = time.monotonic()
            Path('/tmp/maxost.heartbeat').write_text(str(time.time()))
            await asyncio.sleep(5)

    async def close(self):
        self.bridge.stopped = True
        await self.auth.close()
        await self.hub.close()
        await self.media.close()
        await self.tg.close()


async def serve():
    settings = Settings.load()
    db = await Database.connect(
        settings.database_url,
        Vault(settings.encryption_keys),
        settings.workers,
    )
    tg = Telegram(settings.bot_token, settings.telegram_api)
    app = Application(settings, db, tg)
    lock_connection = None
    tasks = []
    try:
        await db.migrate()
        lock_connection = await db.pool.acquire()
        if not await lock_connection.fetchval(
            'SELECT pg_try_advisory_lock(673903102)'
        ):
            raise RuntimeError('Only one MAXOST application instance is supported')
        me = await tg.call('getMe')
        if not me.get('has_topics_enabled'):
            raise RuntimeError(
                'Enable private-chat forum topics in BotFather before starting MAXOST'
            )
        webhook = await tg.call('getWebhookInfo')
        if webhook.get('url'):
            raise RuntimeError(
                'A webhook is configured. Remove it before using long polling'
            )
        await tg.call(
            'setMyCommands',
            {'commands': [
                {'command': 'connect', 'description': 'Подключить MAX'},
                {'command': 'status', 'description': 'Подключение'},
                {'command': 'disconnect', 'description': 'Отключить MAX'},
                {'command': 'cancel', 'description': 'Отменить вход'},
                {'command': 'help', 'description': 'Справка'},
            ]},
        )
        await db.recover()
        await app.hub.restore()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: log.warning(
                'Background task failed (%s)',
                type(context.get('exception')).__name__,
            )
        )
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        log.info(
            'MAXOST started: workers=%d capacity=%d',
            settings.workers,
            settings.max_accounts,
        )
        tasks = [
            asyncio.create_task(app.poll()),
            asyncio.create_task(app.maintenance(lock_connection)),
        ]
        tasks.extend(
            asyncio.create_task(app.bridge.worker())
            for _ in range(settings.workers)
        )
        stop_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is not stop_task:
                task.result()
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.close()
        if lock_connection:
            with contextlib.suppress(Exception):
                await lock_connection.execute(
                    'SELECT pg_advisory_unlock(673903102)'
                )
                await db.pool.release(lock_connection)
        await db.pool.close()
        with contextlib.suppress(FileNotFoundError):
            Path('/tmp/maxost.heartbeat').unlink()


def run():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    for name in ('httpx', 'httpcore', 'aiohttp', 'asyncpg', 'pymax'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.error(
            'MAXOST stopped (%s). Check configuration, BotFather topics, '
            'database and dependency versions.',
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == '__main__':
    run()
