"""Real PostgreSQL tests; run in CI or with TEST_DATABASE_URL (disposable DB only)."""
import asyncio
import base64
import os
import uuid

import pytest
import pytest_asyncio

from maxost.crypto import Vault
from maxost.db import Database
from maxost.errors import Rejected

pytestmark=pytest.mark.integration


@pytest_asyncio.fixture
async def db():
    dsn=os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL is not set (requires disposable PostgreSQL)')
    asyncpg=pytest.importorskip('asyncpg')
    pool=await asyncpg.create_pool(dsn,min_size=2,max_size=12)
    instance=Database(pool,Vault(base64.urlsafe_b64encode(os.urandom(32)).decode()))
    await instance.migrate()
    await pool.execute('TRUNCATE users,auth_attempts,bot_state CASCADE')
    yield instance
    await pool.close()


async def account(db,owner=101,phone='+79990000101'):
    await db.consent(owner)
    a=await db.new_account(owner,phone,100)
    await db.activate(a['id'],owner,owner*100)
    return await db.account(owner)


@pytest.mark.asyncio
async def test_tenant_isolation_and_database_foreign_keys(db):
    import asyncpg
    a=await account(db)
    b=await account(db,102,'+79990000102')
    d=await db.dialog(a,500,700,'Same name')
    e=await db.dialog(b,500,700,'Same name')
    await db.pool.execute("UPDATE dialogs SET tg_thread_id=10,topic_state='ready'")
    assert (await db.by_thread(101,10))['id']==d['id']
    assert (await db.by_thread(102,10))['id']==e['id']
    assert await db.by_thread(103,10) is None
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.pool.execute('INSERT INTO dialogs(id,account_id,owner,max_chat_id,title_cipher) VALUES($1,$2,$3,$4,$5)',uuid.uuid4(),a['id'],102,123,b'bad')


@pytest.mark.asyncio
async def test_unique_ingestion_and_parallel_claims(db):
    a=await account(db)
    d=await db.dialog(a,1,2,'A')
    e=await db.dialog(a,2,3,'B')
    await asyncio.gather(*(db.enqueue(d,'tg','send','m1','',[{'text':'hello'}]) for _ in range(8)))
    assert await db.pool.fetchval('SELECT count(*) FROM jobs')==1
    await db.enqueue(d,'tg','send','m2','',[{'text':'later'}])
    await db.enqueue(e,'tg','send','m3','',[{'text':'other dialog'}])
    claimed=await asyncio.gather(*(db.claim() for _ in range(5)))
    jobs=[x for x in claimed if x]
    assert len(jobs)==2
    assert len({x['id'] for x in jobs})==2
    assert all(x['source_id']!='m2' for x in jobs)
    for job in jobs:
        await db.complete(job,100+job['id'],job['source_id'])
    assert (await db.claim())['source_id']=='m2'


@pytest.mark.asyncio
async def test_unknown_blocks_queue_and_requires_confirmation(db):
    a=await account(db)
    d=await db.dialog(a,1,2,'A')
    await db.enqueue(d,'max','send','10','',[{'text':'first'}])
    await db.enqueue(d,'max','send','11','',[{'text':'second'}])
    job=await db.claim()
    await db.mark_sending(job)
    await db.recover()
    assert await db.claim() is None
    with pytest.raises(Rejected):
        await db.queue_control(102,job['id'],'retry',True)
    with pytest.raises(Rejected):
        await db.queue_control(101,job['id'],'retry')
    await db.queue_control(101,job['id'],'retry',True)
    assert (await db.claim())['id']==job['id']


@pytest.mark.asyncio
async def test_rollback_ownership_dedup_and_payload_erasure(db):
    a=await account(db)
    d=await db.dialog(a,1,2,'A')
    await db.enqueue(d,'tg','send','max:42','',[{'text':'sensitive'}])
    job=await db.claim()
    assert b'sensitive' not in bytes(job['payload'])
    assert db.payload(job)['text']=='sensitive'
    await db.complete(job,70,'max:42')
    assert await db.pool.fetchval('SELECT payload FROM jobs WHERE id=$1',job['id']) is None
    assert (await db.links(d,'tg',70))[0]['max_message_id']=='max:42'
    await db.enqueue(d,'tg','send','max:42','',[{'text':'repeat'}])
    assert await db.pool.fetchval('SELECT count(*) FROM jobs')==1
    await db.pool.execute('DELETE FROM accounts WHERE id=$1 AND owner=$2',a['id'],101)
    assert await db.pool.fetchval('SELECT count(*) FROM jobs')==0
    assert await db.pool.fetchval('SELECT count(*) FROM dialogs')==0


@pytest.mark.asyncio
async def test_session_store_roundtrip(db):
    pytest.importorskip('pymax')
    from pymax.session import SessionInfo
    from maxost.session_store import PostgresSessionStore
    a=await account(db)
    store=PostgresSessionStore(db,a['id'],101)
    session=SessionInfo(token='token-test',device_id='device-test',phone='+79990000101',mt_instance_id='instance-test')
    await store.save_session(session)
    assert (await store.load_session()).token=='token-test'
    assert await store.load_session_by_phone('+70000000000') is None
    await store.update_token('token-test','new-token')
    assert (await store.load_session_by_device_id('device-test')).token=='new-token'
    await store.delete_session('wrong-token')
    assert await store.load_session() is not None
    await store.delete_session('new-token')
    assert await store.load_session() is None


@pytest.mark.asyncio
async def test_album_buffer_survives_restart_and_deduplicates_members(db):
    from maxost.content import from_telegram
    a = await account(db)
    d = await db.dialog(a, 500, 600, 'Group')
    def member(mid, text=''):
        return from_telegram({'message_id': mid, 'caption': text, 'media_group_id': 'a1',
            'photo': [{'file_id': str(mid)}]}, 100000)[0]
    await asyncio.gather(*(db.collect_album(d, 'a1', mid, member(mid), 1) for mid in (12, 11)))
    await db.collect_album(d, 'a1', 11, member(11), 1)
    assert await db.pool.fetchval('SELECT generation FROM albums') == 2
    raw = bytes(await db.pool.fetchval('SELECT body FROM albums'))
    assert b'file_id' not in raw
    # A newly constructed service shares only persisted state with its predecessor.
    restarted = Database(db.pool, db.vault)
    await db.pool.execute("UPDATE albums SET ready_at=now()-interval '1 second'")
    await asyncio.gather(restarted.flush_albums(), db.flush_albums())
    job = await db.claim()
    assert job['source_id'] == 'album:a1'
    assert db.payload(job)['tg_ids'] == [11, 12]
    assert len(db.payload(job)['attachments']) == 2
    await db.complete(job)
    await db.collect_album(d, 'a1', 11, member(11, 'new caption'), 2)
    await db.collect_album(d, 'a1', 11, member(11, 'stale'), 1)
    await db.pool.execute("UPDATE albums SET ready_at=now()-interval '1 second'")
    edit = await db.claim()
    assert edit['action'] == 'edit'
    assert db.payload(edit)['parts'][0]['text'] == 'new caption'
    assert await db.pool.fetchval('SELECT count(*) FROM jobs') == 2


@pytest.mark.asyncio
async def test_album_one_to_many_links_edit_and_encrypted_checkpoints(db):
    import asyncpg
    a = await account(db)
    await account(db, 102, '+79990000102')
    d = await db.dialog(a, 500, 600, 'Group')
    await db.enqueue(d, 'tg', 'send', '55', '', [{'text': ''}])
    job = await db.claim()
    pairs = [{'tg': 21+n, 'max': '55', 'kind': 'photo', 'part': n, 'album': 'album9', 'media_tag': 'hash'} for n in range(3)]
    await db.save_links(job, pairs)
    await db.save_links(job, pairs)
    assert len(await db.links(d, 'max', '55')) == 3
    await db.save_step(job, 'send:0', {'ids': [21, 22, 23], 'secret': 'checkpoint-private'})
    assert (await db.saved_step(job, 'send:0'))['ids'] == [21, 22, 23]
    assert b'checkpoint-private' not in bytes(await db.pool.fetchval('SELECT result FROM delivery_steps'))
    assert await db.saved_step({**dict(job), 'owner': 102}, 'send:0') is None
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.save_step({**dict(job), 'owner': 102}, 'other', {'bad': True})
    await db.complete(job)
    await db.enqueue(d, 'tg', 'edit', '55', 'v2', [{'parts': [{'text': 'new'}]}])
    edit = await db.claim()
    await db.save_links(edit, pairs)
    assert len(await db.source_links(d, 'max', '55')) == 3
    rows = await db.source_links(d, 'max', '55')
    assert all(row['job_id'] == edit['id'] for row in rows)
    assert rows[0]['media_tag'] == 'hash'
    await db.remove_link(d, rows[2]['id'])
    assert len(await db.links(d, 'max', '55')) == 2


@pytest.mark.asyncio
async def test_content_cards_owner_fk_and_token_revocation(db):
    import asyncpg
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from maxost.max_client import MaxHub, Connection
    a = await account(db)
    await account(db, 102, '+79990000102')
    d = await db.dialog(a, 1, 2, 'A')
    data = {'title': 'private poll', 'answers': []}
    card = await db.put_card(d, '44', 'poll', data)
    assert db.card_data(card) == data
    assert b'private poll' not in bytes(card['data'])
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.put_card({**dict(d), 'owner': 102}, '44', 'poll', data)
    await db.pool.execute('UPDATE accounts SET session_cipher=$2 WHERE id=$1', a['id'], b'old token')
    hub = MaxHub(db, SimpleNamespace())
    entry = Connection(a, client=SimpleNamespace(close=AsyncMock()))
    entry.ready.set()
    await hub.reauth(entry)
    current = await db.account(101)
    assert current['status'] == 'reauth' and current['session_cipher'] is None
    assert current['reauth_notified'] is False and entry.closing
    entry.client.close.assert_awaited_once()
    await db.pool.execute('UPDATE accounts SET reauth_notified=true WHERE id=$1', a['id'])
    await hub.reauth(entry)
    assert (await db.account(101))['reauth_notified'] is True
    # Restart must not resume this account or issue SMS.
    await db.recover()
    await hub.restore()
    assert a['id'] not in hub.connections
    await hub.close()


@pytest.mark.asyncio
async def test_v01_rows_survive_forward_migration(db):
    from pathlib import Path
    schema = 'upgrade_' + uuid.uuid4().hex
    async with db.pool.acquire() as c:
        await c.execute(f'CREATE SCHEMA {schema}')
        try:
            await c.execute(f'SET search_path TO {schema}')
            root = Path(__file__).parents[1] / 'maxost' / 'migrations'
            await c.execute((root / '001_initial.sql').read_text())
            aid, did = uuid.uuid4(), uuid.uuid4()
            await c.execute('INSERT INTO users(id) VALUES(1)')
            await c.execute("INSERT INTO accounts(id,owner,phone_hash,phone_cipher,since_ms,history_ms) VALUES($1,1,'h',$2,0,0)", aid, b'encrypted')
            await c.execute('INSERT INTO dialogs(id,account_id,owner,max_chat_id,title_cipher) VALUES($1,$2,1,20,$3)', did, aid, b'title')
            jid = await c.fetchval("INSERT INTO jobs(owner,dialog_id,direction,action,source_id,part) VALUES(1,$1,'tg','send','40',2) RETURNING id", did)
            await c.execute("INSERT INTO message_links(job_id,owner,dialog_id,tg_message_id,max_message_id,part,origin) VALUES($1,1,$2,30,'40',2,'max')", jid, did)
            await c.execute((root / '002_content.sql').read_text())
            row = await c.fetchrow('SELECT * FROM message_links')
            assert row['source_key'] == '40' and row['part'] == 2
            assert row['tg_message_id'] == 30 and row['max_message_id'] == '40'
        finally:
            await c.execute('RESET search_path')
            await c.execute(f'DROP SCHEMA {schema} CASCADE')
