"""Delivery recovery UX and queue isolation, including real PostgreSQL races."""
import asyncio
import base64
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from maxost.crypto import Vault
from maxost.db import Database
from maxost.errors import Rejected
from maxost.queue_actions import QueueActions, action_token, error_text, job_version
from maxost.telegram import TelegramRejected


def key():
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def sample(**changes):
    return {
        'id': 2**63-1, 'owner': 101, 'direction': 'tg', 'status': 'failed',
        'updated_at': datetime.now(timezone.utc), 'attempts': 2, 'payload': b'encrypted',
        'source_id': '42', 'tg_thread_id': 7, 'error': 'File unavailable', **changes,
    }


def ui(db):
    return QueueActions(db, NS(call=AsyncMock(return_value={'message_id': 91}), pace=AsyncMock()))


def query(actions, job, action='retry', owner=101):
    return {
        'id': uuid.uuid4().hex, 'from': {'id': owner},
        'message': {'message_id': 91, 'message_thread_id': 7,
                    'chat': {'id': owner, 'type': 'private'}},
        'data': f"dq:{job['id']}:{action}:{action_token(actions.db.vault, job, action)}",
    }


def test_buttons_have_requested_styles_and_bounded_payloads():
    actions = ui(NS(vault=Vault(key())))
    for confirm in (False, True):
        buttons = actions.markup(sample(status='unknown'), confirm)['inline_keyboard'][0]
        assert [button['style'] for button in buttons] == ['primary', 'danger']
        assert buttons[1]['text'] == 'Пропустить'
        assert 'Повторить' in buttons[0]['text']
        assert all(len(button['text']) <= 64 and len(button['callback_data'].encode()) <= 64 for button in buttons)


def test_finished_and_expired_jobs_do_not_offer_retry():
    actions = ui(NS(vault=Vault(key())))
    assert actions.markup(sample(status='sent')) == {'inline_keyboard': []}
    assert len(actions.markup(sample(payload=None))['inline_keyboard'][0]) == 1
    assert 'срок хранения' in error_text(sample(payload=None))
    assert 'дубль' in error_text(sample(status='unknown'), confirm=True)


@pytest.mark.asyncio
async def test_callback_routing_and_removed_commands():
    from maxost.main import Application
    app = object.__new__(Application)
    app.settings = NS(allowed_users=set())
    app.bridge = NS(queue_actions=NS(callback=AsyncMock()))
    app.tg = NS(call=AsyncMock())
    q = {'id': 'q', 'from': {'id': 101}, 'message': {'chat': {'id': 101, 'type': 'private'}}, 'data': 'dq:1:retry:proof'}
    await app.handle({'callback_query': q})
    app.bridge.queue_actions.callback.assert_awaited_once_with(q)
    for text in ('/retry 1', '/skip 1', '/retry 1 confirm'):
        with pytest.raises(Rejected, match='Неизвестная команда'):
            await app.command(101, text)


@pytest.mark.asyncio
async def test_notification_is_separate_and_does_not_touch_relay_payload():
    job = sample(direction='max')
    db = NS(vault=Vault(key()), pool=NS(execute=AsyncMock()))
    actions = ui(db)
    await actions.notify(job)
    method, params = actions.tg.call.await_args.args
    assert method == 'sendMessage' and params['message_thread_id'] == 7
    assert params['reply_parameters']['message_id'] == 42
    assert 'Повторить' == params['reply_markup']['inline_keyboard'][0][0]['text']
    assert '/retry' not in params['text'] and '/skip' not in params['text']
    assert job['payload'] == b'encrypted'
    assert db.pool.execute.await_args.args[1:] == (
        job['id'], 101, 'failed', job['updated_at'], job['attempts'],
    )


@pytest.mark.asyncio
async def test_missing_topic_notice_can_be_used_in_private_root():
    db = NS(vault=Vault(key()), pool=NS(execute=AsyncMock()))
    actions = ui(db)
    sent = []
    async def call(method, params):
        sent.append(dict(params))
        if len(sent) == 1:
            raise TelegramRejected(400, 'Bad Request: message thread not found')
        return {'message_id': 91}
    actions.tg.call.side_effect = call
    await actions.notify(sample(direction='max'))
    assert sent[0]['message_thread_id'] == 7
    assert 'message_thread_id' not in sent[1] and 'reply_parameters' not in sent[1]
    assert sent[1]['chat_id'] == 101 and sent[1]['reply_markup'] == sent[0]['reply_markup']


@pytest.mark.asyncio
async def test_notice_failure_does_not_acknowledge_or_stop_other_notices():
    from maxost.bridge import Bridge
    rows = [sample(id=1), sample(id=2)]
    bridge = object.__new__(Bridge)
    bridge.db = NS(pool=NS(fetch=AsyncMock(side_effect=[[], rows])))
    bridge.queue_actions = NS(notify=AsyncMock(side_effect=[TelegramRejected(403, 'Forbidden'), None]))
    await bridge.notify_errors()
    assert bridge.queue_actions.notify.await_count == 2


@pytest_asyncio.fixture
async def store():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Requires disposable PostgreSQL')
    import asyncpg
    schema = 'delivery_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA {schema}')
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=8,
                                        server_settings={'search_path': schema})
        db = Database(pool, Vault(key()))
        await db.migrate()
        await db.consent(101)
        account = await db.new_account(101, '+79990000101', 100)
        await db.activate(account['id'], 101, 1001)
        account = await db.account(101)
        dialog = await db.dialog(account, 500, 501, 'Test')
        await db.pool.execute('UPDATE dialogs SET tg_thread_id=7,topic_state=\'ready\'')
        yield db, dialog
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await admin.close()


async def failed_pair(db, dialog, direction='tg', status='failed'):
    await db.enqueue(dialog, direction, 'send', '10', '', [{'text': 'first'}])
    await db.enqueue(dialog, direction, 'send', '11', '', [{'text': 'next'}])
    job = await db.claim()
    await db.mark_sending(job)
    await db.state(job, status, 'test failure')
    return await db.pool.fetchrow('SELECT * FROM jobs WHERE id=$1', job['id'])


@pytest.mark.integration
@pytest.mark.parametrize('status', ['failed', 'unknown'])
@pytest.mark.asyncio
async def test_inbound_failure_does_not_block_next_message(store, status):
    db, dialog = store
    first = await failed_pair(db, dialog, status=status)
    second = await db.claim()
    assert second is not None and second['source_id'] == '11'
    await db.complete(second)
    assert await db.claim() is None
    saved = await db.pool.fetchrow('SELECT * FROM jobs WHERE id=$1', first['id'])
    assert saved['status'] == status and db.payload(saved)['text'] == 'first'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delayed_inbound_retry_does_not_block_ready_messages(store):
    db, dialog = store
    first = await failed_pair(db, dialog)
    await db.state(first, 'pending', 'network delay', 60)
    assert (await db.claim())['source_id'] == '11'


@pytest.mark.integration
@pytest.mark.parametrize('status', ['claimed', 'sending'])
@pytest.mark.asyncio
async def test_inflight_inbound_delivery_preserves_order(store, status):
    db, dialog = store
    first = await failed_pair(db, dialog)
    await db.state(first, status)
    assert await db.claim() is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_outbound_failure_still_waits_for_owner_then_skip_unblocks(store):
    db, dialog = store
    first = await failed_pair(db, dialog, direction='max')
    assert await db.claim() is None
    actions = ui(db)
    await actions.callback(query(actions, first, 'skip'))
    assert (await db.claim())['source_id'] == '11'
    assert await db.pool.fetchval('SELECT payload FROM jobs WHERE id=$1', first['id']) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retry_of_older_id_does_not_overlap_newer_inflight_delivery(store):
    db, dialog = store
    first = await failed_pair(db, dialog)
    second = await db.claim()
    actions = ui(db)
    await actions.callback(query(actions, first))
    assert await db.claim() is None
    await db.complete(second)
    claims = await asyncio.gather(*(db.claim() for _ in range(6)))
    active = [row for row in claims if row is not None]
    assert len(active) == 1 and active[0]['id'] == first['id']


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restart_retry_preserves_checkpoints_and_rejects_double_tap(store):
    db, dialog = store
    first = await failed_pair(db, dialog)
    await db.save_step(first, 'send:0', [{'id': 333}])
    previous = ui(db)
    q = query(previous, first)
    restarted = ui(Database(db.pool, db.vault))
    await restarted.callback(q)
    with pytest.raises(Rejected):
        await restarted.callback(q)
    assert await db.pool.fetchval('SELECT count(*) FROM jobs') == 2
    assert await db.saved_step(first, 'send:0') == [{'id': 333}]
    assert restarted.tg.call.await_args.args[1]['reply_markup'] == {'inline_keyboard': []}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_delivery_requires_signed_confirmation(store):
    db, dialog = store
    first = await failed_pair(db, dialog, status='unknown')
    actions = ui(db)
    q = query(actions, first)
    forged = {**q, 'data': q['data'].replace(':retry:', ':confirm:')}
    with pytest.raises(Rejected):
        await actions.callback(forged)
    await actions.callback(q)
    assert await db.pool.fetchval('SELECT status FROM jobs WHERE id=$1', first['id']) == 'unknown'
    params = actions.tg.call.await_args.args[1]
    assert 'дубль' in params['text']
    confirm = params['reply_markup']['inline_keyboard'][0][0]
    assert confirm['style'] == 'primary' and ':confirm:' in confirm['callback_data']
    await actions.callback({**q, 'id': 'confirm', 'data': confirm['callback_data']})
    assert await db.pool.fetchval('SELECT status FROM jobs WHERE id=$1', first['id']) == 'pending'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_foreign_tampered_and_stale_controls_do_not_change_job(store):
    db, dialog = store
    first = await failed_pair(db, dialog)
    actions = ui(db)
    original = query(actions, first)
    foreign = query(actions, first, owner=202)
    changed_action = {**original, 'data': original['data'].replace(':retry:', ':skip:')}
    for q in (foreign, changed_action, {**original, 'data': 'dq:1:retry:'+'0'*24}):
        with pytest.raises(Rejected):
            await actions.callback(q)
    assert await db.pool.fetchval('SELECT status FROM jobs WHERE id=$1', first['id']) == 'failed'
    await db.state(first, 'failed', 'new failure')
    with pytest.raises(Rejected):
        await actions.callback(original)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_database_compare_and_swap_rejects_stale_failure_version(store):
    db, dialog = store
    first = await failed_pair(db, dialog)
    expected = job_version(first)
    await db.state(first, 'unknown', 'new state')
    with pytest.raises(Rejected):
        await db.queue_control(101, first['id'], 'skip', expected=expected)
    assert await db.pool.fetchval('SELECT status FROM jobs WHERE id=$1', first['id']) == 'unknown'
