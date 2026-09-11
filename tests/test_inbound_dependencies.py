"""Only independent inbound sources may overtake unresolved delivery jobs."""
import asyncio
import base64
import os
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from maxost.crypto import Vault
from maxost.db import Database
from maxost.delivery import Delivery
from maxost.errors import Rejected
from maxost.queue_actions import job_version

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def queue_store():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Requires disposable PostgreSQL')
    import asyncpg

    schema = 'dependencies_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA {schema}')
        pool = await asyncpg.create_pool(
            dsn, min_size=2, max_size=8, server_settings={'search_path': schema},
        )
        db = Database(pool, Vault(base64.urlsafe_b64encode(os.urandom(32)).decode()))
        await db.migrate()
        await db.consent(101)
        account = await db.new_account(101, '+79990000101', 100)
        await db.activate(account['id'], 101, 1001)
        dialog = await db.dialog(await db.account(101), 500, 501, 'Test')
        await pool.execute(
            "UPDATE dialogs SET tg_thread_id=7,topic_state='ready' WHERE id=$1",
            dialog['id'],
        )
        yield db, await db.by_thread(101, 7)
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await admin.close()


async def unresolved_send(db, dialog, status='failed', partial=False):
    await db.enqueue(dialog, 'tg', 'send', '10', '', [{'text': 'original'}])
    first = await db.claim()
    assert first['source_id'] == '10' and first['action'] == 'send'
    await db.mark_sending(first)
    if partial:
        await db.save_links(first, [
            {'tg': 9000, 'max': '10', 'kind': 'photo', 'part': 0, 'album': 'partial'},
        ])
    await db.state(first, status, 'File unavailable', 3600 if status == 'pending' else 0)
    return await db.pool.fetchrow('SELECT * FROM jobs WHERE id=$1', first['id'])


async def enqueue_change(db, dialog, action, source='10', revision='1'):
    payload = {'parts': [{'text': 'edited'}]} if action == 'edit' else {}
    await db.enqueue(dialog, 'tg', action, source, revision, [payload])


@pytest.mark.parametrize('status', ['failed', 'unknown', 'pending'])
@pytest.mark.parametrize('action', ['edit', 'delete', 'reaction', 'poll_refresh'])
@pytest.mark.parametrize('partial', [False, True])
async def test_source_changes_wait_but_unrelated_sends_continue(
    queue_store, status, action, partial,
):
    db, dialog = queue_store
    first = await unresolved_send(db, dialog, status, partial)
    await enqueue_change(db, dialog, action)
    # Also exercises dialog selection when only blocked dependent work remains.
    assert await db.claim() is None

    await db.enqueue(dialog, 'tg', 'send', '11', '', [{'text': 'independent'}])
    independent = await db.claim()
    assert independent is not None and independent['source_id'] == '11'
    await db.complete(independent)
    assert await db.claim() is None
    assert await db.pool.fetchval(
        'SELECT status FROM jobs WHERE id=$1', first['id'],
    ) == status
    assert await db.pool.fetchval(
        "SELECT status FROM jobs WHERE source_id='10' AND action=$1", action,
    ) == 'pending'


async def test_blocked_dialog_does_not_starve_another_with_same_source_id(queue_store):
    db, dialog = queue_store
    await unresolved_send(db, dialog)
    await enqueue_change(db, dialog, 'edit')
    other = await db.dialog(await db.account(101), 600, 601, 'Other')
    await db.enqueue(other, 'tg', 'send', '10', '', [{'text': 'other chat'}])
    claimed = await db.claim()
    assert claimed is not None and claimed['dialog_id'] == other['id']
    await db.complete(claimed)
    assert await db.claim() is None


async def test_failed_send_does_not_block_changes_to_an_independent_sent_source(queue_store):
    db, dialog = queue_store
    await db.enqueue(dialog, 'tg', 'send', '11', '', [{'text': 'delivered'}])
    delivered = await db.claim()
    await db.complete(delivered, 8000, '11')
    await unresolved_send(db, dialog)
    await enqueue_change(db, dialog, 'delete')
    await enqueue_change(db, dialog, 'edit', source='11')
    claimed = await db.claim()
    assert claimed is not None and (claimed['source_id'], claimed['action']) == ('11', 'edit')
    await db.complete(claimed)
    assert await db.claim() is None


async def test_failed_edit_blocks_later_delete_even_when_source_links_exist(queue_store):
    db, dialog = queue_store
    await db.enqueue(dialog, 'tg', 'send', '10', '', [{'text': 'original'}])
    first = await db.claim()
    await db.complete(first, 8000, '10')
    await enqueue_change(db, dialog, 'edit')
    edit = await db.claim()
    await db.state(edit, 'failed', 'Cannot edit')
    await enqueue_change(db, dialog, 'delete')
    await db.enqueue(dialog, 'tg', 'send', '11', '', [{'text': 'independent'}])
    assert len(await db.links(dialog, 'max', '10')) == 1
    independent = await db.claim()
    assert independent['source_id'] == '11'
    await db.complete(independent)
    assert await db.claim() is None


class TelegramMessages:
    """Fake remote side only; queue, checkpoints and source links use PostgreSQL."""
    def __init__(self):
        self.messages, self.calls = {}, []
        self.next_id = 100

    async def pace(self, owner):
        pass

    async def call(self, method, params, files=None):
        self.calls.append(method)
        if method == 'sendMessage':
            self.next_id += 1
            self.messages[self.next_id] = params['text']
            return {'message_id': self.next_id}
        mid = params['message_id']
        assert mid in self.messages
        if method == 'editMessageText':
            self.messages[mid] = params['text']
            return {'message_id': mid}
        assert method == 'deleteMessage'
        del self.messages[mid]
        return True


def delivery_for(db):
    telegram = TelegramMessages()
    bridge = NS(db=db, tg=telegram, media=NS(), topic=AsyncMock(return_value=7))
    return Delivery(bridge), telegram


async def deliver_and_complete(db, delivery, dialog, job):
    assert job is not None
    if job['action'] == 'send':
        await delivery.send(NS(client=NS()), dialog, job, db.payload(job))
    else:
        await delivery.change(NS(client=NS()), dialog, job, db.payload(job))
    await db.complete(job)


@pytest.mark.parametrize('status', ['failed', 'unknown'])
async def test_retry_replays_send_edit_delete_in_order_without_resurrection(queue_store, status):
    db, dialog = queue_store
    first = await unresolved_send(db, dialog, status)
    await enqueue_change(db, dialog, 'edit')
    await enqueue_change(db, dialog, 'delete')
    await db.enqueue(dialog, 'tg', 'send', '11', '', [{'text': 'independent'}])
    delivery, telegram = delivery_for(db)
    independent = await db.claim()
    assert independent['source_id'] == '11'
    await deliver_and_complete(db, delivery, dialog, independent)
    assert await db.claim() is None
    assert not await db.links(dialog, 'max', '10')

    # A restart preserves the dependency chain; UI retry uses this same transaction.
    restarted = Database(db.pool, db.vault)
    await restarted.queue_control(
        101, first['id'], 'retry', expected=job_version(first), confirm=status == 'unknown',
    )
    resent = await restarted.claim()
    assert resent['id'] == first['id']
    await deliver_and_complete(restarted, delivery, dialog, resent)
    edit = await restarted.claim()
    assert edit['action'] == 'edit' and edit['source_id'] == '10'
    await deliver_and_complete(restarted, delivery, dialog, edit)
    assert sorted(telegram.messages.values()) == ['edited', 'independent']
    deletion = await restarted.claim()
    assert deletion['action'] == 'delete' and deletion['source_id'] == '10'
    await deliver_and_complete(restarted, delivery, dialog, deletion)
    assert list(telegram.messages.values()) == ['independent']
    assert telegram.calls == ['sendMessage', 'sendMessage', 'editMessageText', 'deleteMessage']
    assert not await restarted.links(dialog, 'max', '10')
    assert await restarted.claim() is None
    assert await db.pool.fetchval("SELECT count(*) FROM jobs WHERE status<>'sent'") == 0


async def test_skip_original_never_imports_dependent_changes_or_allows_retry(queue_store):
    db, dialog = queue_store
    first = await unresolved_send(db, dialog)
    await enqueue_change(db, dialog, 'edit')
    await enqueue_change(db, dialog, 'delete')
    assert await db.claim() is None
    await db.queue_control(101, first['id'], 'skip', expected=job_version(first))
    delivery, telegram = delivery_for(db)
    for action in ('edit', 'delete'):
        change = await db.claim()
        assert change['action'] == action
        await deliver_and_complete(db, delivery, dialog, change)
    assert telegram.calls == []
    with pytest.raises(Rejected):
        await db.queue_control(101, first['id'], 'retry', expected=job_version(first))
    assert await db.claim() is None


async def test_parallel_claims_cannot_run_change_before_retried_send(queue_store):
    db, dialog = queue_store
    first = await unresolved_send(db, dialog)
    await enqueue_change(db, dialog, 'edit')
    await enqueue_change(db, dialog, 'delete')
    await db.queue_control(101, first['id'], 'retry', expected=job_version(first))
    claims = await asyncio.gather(*(db.claim() for _ in range(6)))
    active = [job for job in claims if job is not None]
    assert len(active) == 1 and active[0]['id'] == first['id']
    await db.complete(active[0], 8000, '10')
    claims = await asyncio.gather(*(db.claim() for _ in range(6)))
    active = [job for job in claims if job is not None]
    assert len(active) == 1 and active[0]['action'] == 'edit'
    assert await db.pool.fetchval("SELECT status FROM jobs WHERE action='delete'") == 'pending'


async def test_recover_unknown_send_retains_dependent_changes(queue_store):
    db, dialog = queue_store
    first = await unresolved_send(db, dialog)
    await db.state(first, 'sending')
    await enqueue_change(db, dialog, 'edit')
    await enqueue_change(db, dialog, 'delete')
    await db.enqueue(dialog, 'tg', 'send', '11', '', [{'text': 'independent'}])
    await db.recover()
    restarted = Database(db.pool, db.vault)
    independent = await restarted.claim()
    assert independent['source_id'] == '11'
    await restarted.complete(independent)
    assert await restarted.claim() is None
    assert await db.pool.fetchval('SELECT status FROM jobs WHERE id=$1', first['id']) == 'unknown'
