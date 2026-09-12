"""Real PostgreSQL tests; run in CI or with TEST_DATABASE_URL."""
import asyncio
import base64
import os
import uuid

import pytest
import pytest_asyncio

from maxost.crypto import Vault
from maxost.db import Database
from maxost.errors import Rejected
from maxost.queue_actions import job_version

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def db():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL is not set (requires disposable PostgreSQL)')
    asyncpg = pytest.importorskip('asyncpg')
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=12)
    instance = Database(
        pool,
        Vault(base64.urlsafe_b64encode(os.urandom(32)).decode()),
    )
    await instance.migrate()
    await pool.execute('TRUNCATE users,auth_attempts,bot_state CASCADE')
    yield instance
    await pool.close()


async def account(db, owner=101, phone='+79990000101'):
    await db.consent(owner)
    created = await db.new_account(owner, phone, 100)
    await db.activate(created['id'], owner, owner * 100)
    return await db.account(owner)


@pytest.mark.asyncio
async def test_tenant_isolation_and_database_foreign_keys(db):
    import asyncpg

    first = await account(db)
    second = await account(db, 102, '+79990000102')
    dialog_a = await db.dialog(first, 500, 700, 'Same name')
    dialog_b = await db.dialog(second, 500, 700, 'Same name')
    await db.pool.execute("UPDATE dialogs SET tg_thread_id=10,topic_state='ready'")
    assert (await db.by_thread(101, 10))['id'] == dialog_a['id']
    assert (await db.by_thread(102, 10))['id'] == dialog_b['id']
    assert await db.by_thread(103, 10) is None
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.pool.execute(
            '''INSERT INTO dialogs(id,account_id,owner,max_chat_id,title_cipher)
            VALUES($1,$2,$3,$4,$5)''',
            uuid.uuid4(), first['id'], 102, 123, b'bad',
        )


@pytest.mark.asyncio
async def test_unique_ingestion_and_parallel_claims(db):
    created = await account(db)
    dialog_a = await db.dialog(created, 1, 2, 'A')
    dialog_b = await db.dialog(created, 2, 3, 'B')
    await asyncio.gather(*(
        db.enqueue(dialog_a, 'tg', 'send', 'm1', '', [{'text': 'hello'}])
        for _ in range(8)
    ))
    assert await db.pool.fetchval('SELECT count(*) FROM jobs') == 1
    await db.enqueue(dialog_a, 'tg', 'send', 'm2', '', [{'text': 'later'}])
    await db.enqueue(dialog_b, 'tg', 'send', 'm3', '', [{'text': 'other'}])
    claimed = await asyncio.gather(*(db.claim() for _ in range(5)))
    jobs = [item for item in claimed if item]
    assert len(jobs) == 2
    assert len({item['id'] for item in jobs}) == 2
    assert all(item['source_id'] != 'm2' for item in jobs)
    for job in jobs:
        await db.complete(job, 100 + job['id'], job['source_id'])
    assert (await db.claim())['source_id'] == 'm2'


@pytest.mark.asyncio
async def test_unknown_blocks_outbound_queue_and_requires_confirmation(db):
    created = await account(db)
    dialog = await db.dialog(created, 1, 2, 'A')
    await db.enqueue(dialog, 'max', 'send', '10', '', [{'text': 'first'}])
    await db.enqueue(dialog, 'max', 'send', '11', '', [{'text': 'second'}])
    job = await db.claim()
    await db.mark_sending(job)
    await db.recover()
    assert await db.claim() is None
    failed = await db.pool.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])
    expected = job_version(failed)
    with pytest.raises(Rejected):
        await db.queue_control(102, job['id'], 'retry', expected=expected, confirm=True)
    with pytest.raises(Rejected):
        await db.queue_control(101, job['id'], 'retry', expected=expected)
    await db.queue_control(101, job['id'], 'retry', expected=expected, confirm=True)
    assert (await db.claim())['id'] == job['id']


@pytest.mark.asyncio
async def test_rollback_ownership_dedup_and_payload_erasure(db):
    created = await account(db)
    dialog = await db.dialog(created, 1, 2, 'A')
    await db.enqueue(
        dialog, 'tg', 'send', 'max:42', '', [{'text': 'sensitive'}]
    )
    job = await db.claim()
    assert b'sensitive' not in bytes(job['payload'])
    assert db.payload(job)['text'] == 'sensitive'
    await db.complete(job, 70, 'max:42')
    assert await db.pool.fetchval(
        'SELECT payload FROM jobs WHERE id=$1', job['id']
    ) is None
    assert (await db.links(dialog, 'tg', 70))[0]['max_message_id'] == 'max:42'
    await db.enqueue(
        dialog, 'tg', 'send', 'max:42', '', [{'text': 'repeat'}]
    )
    assert await db.pool.fetchval('SELECT count(*) FROM jobs') == 1
    await db.pool.execute(
        'DELETE FROM accounts WHERE id=$1 AND owner=$2', created['id'], 101
    )
    assert await db.pool.fetchval('SELECT count(*) FROM jobs') == 0
    assert await db.pool.fetchval('SELECT count(*) FROM dialogs') == 0


@pytest.mark.asyncio
async def test_session_store_roundtrip(db):
    pytest.importorskip('pymax')
    from maxost.session_store import PostgresSessionStore
    from pymax.session import SessionInfo

    created = await account(db)
    store = PostgresSessionStore(db, created['id'], 101)
    session = SessionInfo(
        token='token-test',
        device_id='device-test',
        phone='+79990000101',
        mt_instance_id='instance-test',
    )
    await store.save_session(session)
    assert (await store.load_session()).token == 'token-test'
    assert await store.load_session_by_phone('+70000000000') is None
    await store.update_token('token-test', 'new-token')
    assert (await store.load_session_by_device_id('device-test')).token == 'new-token'
    await store.delete_session('wrong-token')
    assert await store.load_session() is not None
    await store.delete_session('new-token')
    assert await store.load_session() is None


@pytest.mark.asyncio
async def test_album_buffer_survives_restart_and_deduplicates_members(db):
    from maxost.content import from_telegram

    created = await account(db)
    dialog = await db.dialog(created, 500, 600, 'Group')

    def member(message_id, text=''):
        return from_telegram(
            {
                'message_id': message_id,
                'caption': text,
                'media_group_id': 'a1',
                'photo': [{'file_id': str(message_id)}],
            },
            100000,
        )[0]

    await asyncio.gather(*(
        db.collect_album(dialog, 'a1', message_id, member(message_id), 1)
        for message_id in (12, 11)
    ))
    await db.collect_album(dialog, 'a1', 11, member(11), 1)
    assert await db.pool.fetchval('SELECT generation FROM albums') == 2
    raw = bytes(await db.pool.fetchval('SELECT body FROM albums'))
    assert b'file_id' not in raw

    restarted = Database(db.pool, db.vault)
    await db.pool.execute("UPDATE albums SET ready_at=now()-interval '1 second'")
    await asyncio.gather(restarted.flush_albums(), db.flush_albums())
    job = await db.claim()
    assert job['source_id'] == 'album:a1'
    assert db.payload(job)['tg_ids'] == [11, 12]
    assert len(db.payload(job)['attachments']) == 2
    await db.complete(job)

    await db.collect_album(dialog, 'a1', 11, member(11, 'new caption'), 2)
    await db.collect_album(dialog, 'a1', 11, member(11, 'stale'), 1)
    await db.pool.execute("UPDATE albums SET ready_at=now()-interval '1 second'")
    edit = await db.claim()
    assert edit['action'] == 'edit'
    assert db.payload(edit)['parts'][0]['text'] == 'new caption'
    assert await db.pool.fetchval('SELECT count(*) FROM jobs') == 2


@pytest.mark.asyncio
async def test_album_links_and_encrypted_checkpoints(db):
    import asyncpg

    created = await account(db)
    await account(db, 102, '+79990000102')
    dialog = await db.dialog(created, 500, 600, 'Group')
    await db.enqueue(dialog, 'tg', 'send', '55', '', [{'text': ''}])
    job = await db.claim()
    pairs = [
        {
            'tg': 21 + index,
            'max': '55',
            'kind': 'photo',
            'part': index,
            'album': 'album9',
            'media_tag': 'hash',
        }
        for index in range(3)
    ]
    await db.save_links(job, pairs)
    await db.save_links(job, pairs)
    assert len(await db.links(dialog, 'max', '55')) == 3
    await db.save_step(
        job,
        'send:0',
        {'ids': [21, 22, 23], 'secret': 'checkpoint-private'},
    )
    assert (await db.saved_step(job, 'send:0'))['ids'] == [21, 22, 23]
    assert b'checkpoint-private' not in bytes(
        await db.pool.fetchval('SELECT result FROM delivery_steps')
    )
    assert await db.saved_step({**dict(job), 'owner': 102}, 'send:0') is None
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.save_step(
            {**dict(job), 'owner': 102}, 'other', {'bad': True}
        )
    await db.complete(job)

    await db.enqueue(
        dialog,
        'tg',
        'edit',
        '55',
        'v2',
        [{'parts': [{'text': 'new'}]}],
    )
    edit = await db.claim()
    await db.save_links(edit, pairs)
    rows = await db.source_links(dialog, 'max', '55')
    assert len(rows) == 3
    assert all(row['job_id'] == edit['id'] for row in rows)
    assert rows[0]['media_tag'] == 'hash'
    await db.remove_link(dialog, rows[2]['id'])
    assert len(await db.links(dialog, 'max', '55')) == 2


@pytest.mark.asyncio
async def test_poll_mirror_owner_fk_and_token_revocation(db):
    import asyncpg
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from maxost.max_client import Connection, MaxHub

    created = await account(db)
    await account(db, 102, '+79990000102')
    dialog = await db.dialog(created, 1, 2, 'A')
    data = {'title': 'private poll', 'answers': []}
    mirror = await db.put_poll(dialog, '44', data)
    assert db.poll_data(mirror) == data
    assert b'private poll' not in bytes(mirror['data'])
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.put_poll({**dict(dialog), 'owner': 102}, '44', data)

    await db.pool.execute(
        'UPDATE accounts SET session_cipher=$2 WHERE id=$1',
        created['id'],
        b'old token',
    )
    hub = MaxHub(db, SimpleNamespace())
    entry = Connection(created, client=SimpleNamespace(close=AsyncMock()))
    entry.ready.set()
    await hub.reauth(entry)
    current = await db.account(101)
    assert current['status'] == 'reauth' and current['session_cipher'] is None
    assert current['reauth_notified'] is False and entry.closing
    entry.client.close.assert_awaited_once()
    await db.pool.execute(
        'UPDATE accounts SET reauth_notified=true WHERE id=$1', created['id']
    )
    await hub.reauth(entry)
    assert (await db.account(101))['reauth_notified'] is True
    await db.recover()
    await hub.restore()
    assert created['id'] not in hub.connections
    await hub.close()


@pytest.mark.asyncio
async def test_repository_has_single_initial_schema(db):
    versions = await db.pool.fetch(
        'SELECT version FROM schema_migrations ORDER BY version'
    )
    assert [row['version'] for row in versions] == ['001_schema.sql']
