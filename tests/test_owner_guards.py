"""Conflict paths must reject EXCLUDED.owner before altering existing data."""
import base64
import os
import uuid

import pytest
import pytest_asyncio

from maxost.crypto import Vault
from maxost.db import Database

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def owned_store():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('TEST_DATABASE_URL is not set')
    asyncpg = pytest.importorskip('asyncpg')
    schema = 'owner_guard_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA {schema}')
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=4,
                                        server_settings={'search_path': schema})
        db = Database(pool, Vault(base64.urlsafe_b64encode(os.urandom(32)).decode()))
        await db.migrate()
        for owner in (101, 102):
            await db.consent(owner)
            a = await db.new_account(owner, f'+79990000{owner}', 100)
            await db.activate(a['id'], owner, owner * 100)
        account = await db.account(101)
        dialog = await db.dialog(account, 1, 2, 'private')
        await db.enqueue(dialog, 'tg', 'send', '77', '', [{'text': 'original'}])
        job = await db.claim()
        yield db, dialog, job
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_card_conflict_rejects_foreign_owner_without_corrupting_ciphertext(owned_store):
    import asyncpg
    db, dialog, _ = owned_store
    card = await db.put_card(dialog, '77', 'poll', {'title': 'original'})
    foreign = {**dict(dialog), 'owner': 102}
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.put_card(foreign, '77', 'poll', {'title': 'wrong owner'})
    row = await db.pool.fetchrow('SELECT * FROM content_cards WHERE id=$1', card['id'])
    assert bytes(row['data']) == bytes(card['data'])
    assert db.card_data(row) == {'title': 'original'}
    updated = await db.put_card(dialog, '77', 'poll', {'title': 'valid update'})
    assert updated['id'] == card['id']
    assert db.card_data(updated) == {'title': 'valid update'}


@pytest.mark.asyncio
async def test_checkpoint_conflict_cannot_silently_accept_foreign_owner(owned_store):
    import asyncpg
    db, _, job = owned_store
    await db.save_step(job, 'send:0', {'message_id': 77})
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.save_step({**dict(job), 'owner': 102}, 'send:0', {'message_id': 88})
    assert await db.saved_step(job, 'send:0') == {'message_id': 77}
    await db.save_step(job, 'send:0', {'message_id': 99})  # Legitimate retry is still idempotent.
    assert await db.saved_step(job, 'send:0') == {'message_id': 77}


@pytest.mark.asyncio
async def test_album_conflict_checks_parent_before_upsert(owned_store):
    import asyncpg
    db, dialog, _ = owned_store
    query = '''INSERT INTO albums(dialog_id,owner,group_id,body) VALUES($1,$2,'g',$3)
               ON CONFLICT(dialog_id,group_id) DO UPDATE SET body=excluded.body'''
    await db.pool.execute(query, dialog['id'], 101, b'original ciphertext')
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.pool.execute(query, dialog['id'], 102, b'wrong ciphertext')
    assert bytes(await db.pool.fetchval('SELECT body FROM albums')) == b'original ciphertext'


@pytest.mark.asyncio
async def test_link_conflict_rejects_foreign_owner_and_rolls_back(owned_store):
    import asyncpg
    db, dialog, job = owned_store
    pair = {'tg': 88, 'max': '77', 'kind': 'text', 'part': 0}
    await db.save_links(job, [pair])
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.save_links({**dict(job), 'owner': 102}, [{**pair, 'max': 'attacker'}])
    rows = await db.links(dialog, 'max', '77')
    assert len(rows) == 1 and rows[0]['owner'] == 101
    assert await db.pool.fetchval('SELECT count(*) FROM message_links') == 1


@pytest.mark.asyncio
async def test_guard_migration_is_forward_only_and_idempotent(owned_store):
    db, dialog, _ = owned_store
    card = await db.put_card(dialog, '77', 'poll', {'title': 'keep'})
    await db.migrate()
    row = await db.pool.fetchrow('SELECT * FROM content_cards WHERE id=$1', card['id'])
    assert db.card_data(row) == {'title': 'keep'}
    assert await db.pool.fetchval("SELECT count(*) FROM schema_migrations WHERE version='003_content_owner_guards.sql'") == 1
