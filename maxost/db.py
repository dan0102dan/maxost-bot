from __future__ import annotations

import time
import uuid
from pathlib import Path

from .content_store import ContentStore
from .errors import Rejected
from .queue_actions import job_version


class Database(ContentStore):
    def __init__(self, pool, vault):
        self.pool, self.vault = pool, vault

    @classmethod
    async def connect(cls, dsn, vault, workers=4):
        import asyncpg

        pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=workers + 8,
            command_timeout=30,
        )
        return cls(pool, vault)

    async def migrate(self):
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute('SELECT pg_advisory_xact_lock(673903100)')
            await connection.execute(
                'CREATE TABLE IF NOT EXISTS schema_migrations '
                '(version TEXT PRIMARY KEY)'
            )
            for file in sorted(Path(__file__).with_name('migrations').glob('*.sql')):
                if not await connection.fetchval(
                    'SELECT 1 FROM schema_migrations WHERE version=$1', file.name
                ):
                    await connection.execute(file.read_text())
                    await connection.execute(
                        'INSERT INTO schema_migrations VALUES($1)', file.name
                    )

    async def consent(self, owner):
        await self.pool.execute(
            'INSERT INTO users(id) VALUES($1) ON CONFLICT DO NOTHING', owner
        )

    async def account(self, owner):
        return await self.pool.fetchrow(
            'SELECT * FROM accounts WHERE owner=$1', owner
        )

    async def new_account(self, owner, phone, capacity):
        account_id = uuid.uuid4()
        fingerprint = self.vault.digest('phone:' + phone)
        now = int(time.time() * 1000)
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute('SELECT pg_advisory_xact_lock(673903101)')
            if await connection.fetchval(
                'SELECT 1 FROM accounts WHERE owner=$1 OR phone_hash=$2',
                owner,
                fingerprint,
            ):
                raise Rejected(
                    'Подключение уже существует. Используйте /status или /disconnect.'
                )
            if await connection.fetchval('SELECT count(*) FROM accounts') >= capacity:
                raise Rejected('Сервис временно не принимает новые подключения.')
            counts = await connection.fetchrow(
                '''SELECT count(*) FILTER(WHERE owner=$1) AS per_user,
                   count(*) FILTER(WHERE phone_hash=$2) AS per_phone,
                   count(*) AS total,
                   count(*) FILTER(WHERE (owner=$1 OR phone_hash=$2)
                       AND created_at>now()-interval '60 seconds') AS recent
                FROM auth_attempts WHERE created_at>now()-interval '1 hour' ''',
                owner,
                fingerprint,
            )
            if (
                counts['per_user'] >= 3
                or counts['per_phone'] >= 3
                or counts['total'] >= 60
                or counts['recent']
            ):
                raise Rejected(
                    'Слишком много попыток входа. Повторите позже (лимит: 3 в час).'
                )
            await connection.execute(
                'INSERT INTO auth_attempts(owner,phone_hash) VALUES($1,$2)',
                owner,
                fingerprint,
            )
            return await connection.fetchrow(
                '''INSERT INTO accounts(
                    id,owner,phone_hash,phone_cipher,since_ms,history_ms)
                VALUES($1,$2,$3,$4,$5,$5) RETURNING *''',
                account_id,
                owner,
                fingerprint,
                self.vault.seal(owner, f'phone:{account_id}', phone),
                now,
            )

    async def activate(self, account_id, owner, max_user_id):
        try:
            result = await self.pool.execute(
                '''UPDATE accounts SET max_user_id=$3,status='connected',
                reauth_notified=false WHERE id=$1 AND owner=$2''',
                account_id,
                owner,
                max_user_id,
            )
        except Exception as exc:
            if getattr(exc, 'sqlstate', None) == '23505':
                raise Rejected(
                    'Этот аккаунт MAX уже подключён к другому пользователю.'
                ) from None
            raise
        if result != 'UPDATE 1':
            raise Rejected('Подключение отменено.')

    async def dialog(self, account, max_chat_id, peer_id, title):
        dialog_id = uuid.uuid4()
        purpose = f"title:{account['id']}:{max_chat_id}"
        return await self.pool.fetchrow(
            '''INSERT INTO dialogs(
                id,account_id,owner,max_chat_id,peer_id,title_cipher)
            VALUES($1,$2,$3,$4,$5,$6)
            ON CONFLICT(account_id,max_chat_id) DO UPDATE SET
                title_cipher=excluded.title_cipher,peer_id=excluded.peer_id
            RETURNING *''',
            dialog_id,
            account['id'],
            account['owner'],
            max_chat_id,
            peer_id,
            self.vault.seal(account['owner'], purpose, title),
        )

    async def by_thread(self, owner, thread):
        return await self.pool.fetchrow(
            'SELECT * FROM dialogs WHERE owner=$1 AND tg_thread_id=$2',
            owner,
            thread,
        )

    async def enqueue(self, dialog, direction, action, source_id, revision, parts):
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.fetchval(
                'SELECT id FROM dialogs WHERE id=$1 AND owner=$2 FOR UPDATE',
                dialog['id'],
                dialog['owner'],
            )
            for index, payload in enumerate(parts):
                purpose = (
                    f"job:{dialog['id']}:{direction}:{action}:"
                    f"{source_id}:{revision}:{index}"
                )
                await connection.execute(
                    '''INSERT INTO jobs(
                        owner,dialog_id,direction,action,source_id,revision,part,payload)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                    ON CONFLICT DO NOTHING''',
                    dialog['owner'],
                    dialog['id'],
                    direction,
                    action,
                    str(source_id),
                    revision,
                    index,
                    self.vault.seal(dialog['owner'], purpose, payload),
                )

    def payload(self, job):
        purpose = (
            f"job:{job['dialog_id']}:{job['direction']}:{job['action']}:"
            f"{job['source_id']}:{job['revision']}:{job['part']}"
        )
        return self.vault.open(job['owner'], purpose, bytes(job['payload']))

    async def recover(self):
        await self.pool.execute(
            "UPDATE jobs SET status='pending' WHERE status='claimed'"
        )
        await self.pool.execute(
            """UPDATE jobs SET status='unknown',notified=false,
            error='Процесс остановился во время отправки; результат неизвестен.'
            WHERE status='sending'"""
        )
        await self.pool.execute(
            "UPDATE dialogs SET topic_state='unknown' WHERE topic_state='creating'"
        )
        await self.pool.execute(
            "UPDATE accounts SET status='offline' WHERE status='connected'"
        )
        await self.pool.execute(
            "DELETE FROM accounts WHERE status='authorizing' AND session_cipher IS NULL"
        )
        await self.pool.execute(
            "UPDATE accounts SET status='reauth' WHERE status='authorizing'"
        )

    async def claim(self):
        await self.flush_albums()
        # Lock the dialog before choosing a job. A retry can requeue an older ID;
        # its claim must never overlap a newer in-flight job in the same direction.
        async with self.pool.acquire() as connection, connection.transaction():
            dialog = await connection.fetchrow(
                '''SELECT d.id FROM dialogs d JOIN accounts a ON a.id=d.account_id
                JOIN LATERAL (
                    SELECT j.id FROM jobs j
                    WHERE j.dialog_id=d.id AND j.owner=d.owner
                    AND j.status='pending' AND j.available_at<=now()
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs active WHERE active.dialog_id=j.dialog_id
                        AND active.direction=j.direction
                        AND active.status IN ('claimed','sending'))
                    AND (j.direction='tg' OR NOT EXISTS (
                        SELECT 1 FROM jobs prev WHERE prev.dialog_id=j.dialog_id
                        AND prev.direction=j.direction AND prev.id<j.id
                        AND prev.status NOT IN ('sent','skipped')))
                    ORDER BY j.id LIMIT 1
                ) ready ON true
                WHERE a.status IN ('connected','offline')
                ORDER BY ready.id FOR UPDATE OF d SKIP LOCKED LIMIT 1'''
            )
            if dialog is None:
                return None
            # READ COMMITTED gets a fresh snapshot after taking the dialog lock.
            return await connection.fetchrow(
                '''WITH next AS (
                    SELECT j.id FROM jobs j
                    WHERE j.dialog_id=$1 AND j.status='pending' AND j.available_at<=now()
                    AND NOT EXISTS (
                        SELECT 1 FROM jobs active WHERE active.dialog_id=j.dialog_id
                        AND active.direction=j.direction
                        AND active.status IN ('claimed','sending'))
                    AND (j.direction='tg' OR NOT EXISTS (
                        SELECT 1 FROM jobs prev WHERE prev.dialog_id=j.dialog_id
                        AND prev.direction=j.direction AND prev.id<j.id
                        AND prev.status NOT IN ('sent','skipped')))
                    ORDER BY j.id FOR UPDATE SKIP LOCKED LIMIT 1
                ) UPDATE jobs SET status='claimed',updated_at=now()
                FROM next WHERE jobs.id=next.id RETURNING jobs.*''', dialog['id'],
            )

    async def state(self, job, status, error=None, delay=0):
        await self.pool.execute(
            '''UPDATE jobs SET status=$3,error=$4,updated_at=now(),
            available_at=now()+($5::double precision*interval '1 second'),
            notified=false WHERE id=$1 AND owner=$2''',
            job['id'],
            job['owner'],
            status,
            error,
            delay,
        )

    async def mark_sending(self, job):
        await self.pool.execute(
            '''UPDATE jobs SET status='sending',attempts=attempts+1,updated_at=now()
            WHERE id=$1 AND owner=$2''',
            job['id'],
            job['owner'],
        )

    async def complete(self, job, tg_id=None, max_id=None, kind='text'):
        if tg_id is not None and max_id is not None and job['action'] == 'send':
            await self.save_links(
                job,
                [{'tg': tg_id, 'max': max_id, 'kind': kind, 'part': 0}],
            )
        await self.pool.execute(
            '''UPDATE jobs SET status='sent',payload=NULL,error=NULL,updated_at=now()
            WHERE id=$1 AND owner=$2''',
            job['id'],
            job['owner'],
        )

    async def links(self, dialog, source, message_id):
        column = {'tg': 'tg_message_id', 'max': 'max_message_id'}[source]
        value = int(message_id) if source == 'tg' else str(message_id)
        return await self.pool.fetch(
            f'''SELECT * FROM message_links WHERE owner=$1 AND dialog_id=$2
            AND {column}=$3 ORDER BY part,id''',
            dialog['owner'],
            dialog['id'],
            value,
        )

    async def queue_control(self, owner, job_id, action, *, expected, confirm=False):
        if action not in ('retry', 'skip'):
            raise Rejected('Действие недоступно.')
        async with self.pool.acquire() as connection, connection.transaction():
            # Same dialog -> job lock order as ingestion, claim and album assembly.
            dialog_id = await connection.fetchval(
                '''SELECT d.id FROM dialogs d JOIN jobs j
                ON j.dialog_id=d.id AND j.owner=d.owner
                WHERE j.id=$1 AND j.owner=$2 FOR UPDATE OF d''', job_id, owner,
            )
            if dialog_id is None:
                raise Rejected('Это действие уже недоступно.')
            job = await connection.fetchrow(
                'SELECT * FROM jobs WHERE owner=$1 AND id=$2 FOR UPDATE',
                owner, job_id,
            )
            if (not job or job['status'] not in ('failed', 'unknown')
                    or job_version(job) != expected):
                raise Rejected('Это действие уже недоступно.')
            if action == 'retry' and job['payload'] is None:
                raise Rejected('Срок хранения истёк. Отправьте сообщение заново.')
            if action == 'retry' and job['status'] == 'unknown' and not confirm:
                raise Rejected('Подтвердите повтор кнопкой: возможен дубль.')
            state = 'pending' if action == 'retry' else 'skipped'
            await connection.execute(
                '''UPDATE jobs SET status=$3,error=NULL,notified=false,
                available_at=now(),updated_at=now(),
                payload=CASE WHEN $3='skipped' THEN NULL ELSE payload END
                WHERE id=$1 AND owner=$2''', job_id, owner, state,
            )

    async def set_offset(self, value):
        await self.pool.execute(
            "INSERT INTO bot_state VALUES('offset',$1) "
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            value,
        )

    async def get_offset(self):
        return await self.pool.fetchval(
            "SELECT value FROM bot_state WHERE key='offset'"
        ) or 0

    async def cleanup(self, retention):
        for table in ('albums', 'poll_mirrors'):
            await self.pool.execute(
                f"DELETE FROM {table} "
                "WHERE updated_at<now()-($1::integer*interval '1 day')",
                retention,
            )
        await self.pool.execute(
            "DELETE FROM auth_attempts WHERE created_at<now()-interval '1 day'"
        )
        await self.pool.execute(
            """DELETE FROM jobs WHERE status IN ('sent','skipped')
            AND updated_at<now()-($1::integer*interval '1 day')""",
            retention,
        )
        await self.pool.execute(
            """UPDATE jobs SET status='skipped',payload=NULL,
            error='Истёк срок хранения недоставленного сообщения',
            notified=false,updated_at=now()
            WHERE status IN ('pending','failed','unknown')
            AND created_at<now()-($1::integer*interval '1 day')""",
            retention,
        )
