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
