"""Database guarantees and application wiring for native interactions."""
import asyncio
import base64
import os
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from maxost.crypto import Vault
from maxost.db import Database
from maxost.native_polls import NativePolls, poll_signature


@pytest_asyncio.fixture
async def native_db():
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Requires disposable PostgreSQL')
    asyncpg = pytest.importorskip('asyncpg')
    pool = await asyncpg.create_pool(dsn)
    db = Database(pool, Vault(base64.urlsafe_b64encode(os.urandom(32)).decode()))
    await db.migrate()
    await pool.execute('TRUNCATE users,auth_attempts,bot_state CASCADE')
    try:
        yield db
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_native_poll_restart_ownership_and_duplicate_update(native_db):
    db = native_db
    await db.consent(101)
    account = await db.new_account(101, '+79990000101', 100)
    await db.activate(account['id'], 101, 10100)
    account = await db.account(101)
    dialog = await db.dialog(account, 400, 500, 'Dialog')
    poll = {
        'poll_id': 100,
        'title': 'Choice',
        'settings': 5,
        'answers': [
            {'answer_id': 11, 'text': 'A'},
            {'answer_id': 35, 'text': 'B'},
        ],
    }
    mirror = await db.put_poll(dialog, '42', poll)
    await db.pool.execute(
        '''UPDATE poll_mirrors SET tg_poll_id=$3,signature=$4,
        tg_message_id=60 WHERE id=$1 AND owner=$2''',
        mirror['id'], 101, 'native-1', poll_signature(poll),
    )
    lock = asyncio.Lock()
    native = NativePolls(
        NS(db=Database(db.pool, db.vault), tg=NS(), lock=lambda _: lock)
    )
    event = {
        'poll_id': 'native-1',
        'user': {'id': 101},
        'option_ids': [1],
        'update_id': 10,
    }
    await native.answer(event)
    await native.answer(event)
    await native.answer({**event, 'user': {'id': 102}, 'update_id': 11})
    rows = await db.pool.fetch('SELECT * FROM jobs')
    assert len(rows) == 1 and rows[0]['owner'] == 101
    assert db.payload(rows[0])['answer_ids'] == [35]
    assert b'answer_ids' not in bytes(rows[0]['payload'])

    second = await db.put_poll(dialog, '43', poll)
    import asyncpg
    with pytest.raises(asyncpg.UniqueViolationError):
        await db.pool.execute(
            'UPDATE poll_mirrors SET tg_poll_id=$3 WHERE id=$1 AND owner=$2',
            second['id'], 101, 'native-1',
        )

    await db.pool.execute(
        'DELETE FROM accounts WHERE id=$1 AND owner=$2', account['id'], 101
    )
    assert await db.pool.fetchval('SELECT count(*) FROM poll_mirrors') == 0


@pytest.mark.asyncio
async def test_application_routes_poll_answers_without_relaying_messages():
    from maxost.main import Application

    app = object.__new__(Application)
    app.settings = NS(allowed_users=set())
    app.tg = NS(text=AsyncMock())
    native = NS(answer=AsyncMock())
    app.bridge = NS(
        interactions=NS(native_polls=native),
        from_telegram=AsyncMock(),
    )
    await app.handle({
        'update_id': 9,
        'poll_answer': {
            'poll_id': 'poll-1',
            'user': {'id': 101},
            'option_ids': [0],
        },
    })
    native.answer.assert_awaited_once_with({
        'update_id': 9,
        'poll_id': 'poll-1',
        'user': {'id': 101},
        'option_ids': [0],
    })
    app.bridge.from_telegram.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_status_is_short_when_healthy():
    from maxost.main import Application

    app = object.__new__(Application)
    app.db = NS(
        account=AsyncMock(return_value={'status': 'connected'}),
        pool=NS(fetch=AsyncMock(return_value=[])),
    )
    app.tg = NS(text=AsyncMock())
    await app.status(101, 7)
    app.tg.text.assert_awaited_once_with(101, '🟢 MAX подключён', 7)
    app.db.pool.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_native_vote_rejects_changed_original_before_mutation():
    from maxost.errors import Rejected
    from maxost.interactions import Interactions

    poll = {
        'poll_id': 100,
        'title': 'Changed',
        'settings': 5,
        'answers': [
            {'answer_id': 11, 'text': 'A'},
            {'answer_id': 35, 'text': 'B'},
        ],
    }

    class Poll:
        type = 'POLL'

        def model_dump(self, **kwargs):
            return poll

    db = NS(links=AsyncMock(return_value=[{'tg_message_id': 60}]))
    bridge = NS(db=db, tg=NS(), delivery=NS(step=AsyncMock()))
    interactions = Interactions(bridge)
    client = NS(
        get_message=AsyncMock(return_value=NS(attaches=[Poll()])),
        vote_poll=AsyncMock(),
    )
    payload = {
        'poll_id': 100,
        'answer_ids': [35],
        'native_signature': 'old',
    }
    with pytest.raises(Rejected):
        await interactions.deliver(
            NS(client=client),
            {'owner': 101, 'max_chat_id': 400},
            {'action': 'vote', 'source_id': '42'},
            payload,
        )
    bridge.delivery.step.assert_not_awaited()
    client.vote_poll.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbound_telegram_poll_is_not_echoed_back(monkeypatch):
    from maxost import max_transport
    from maxost.delivery import Delivery

    poll = {
        'poll_id': 100,
        'title': 'A?',
        'settings': 5,
        'answers': [{'answer_id': 1, 'text': 'Yes'}],
    }
    monkeypatch.setattr(
        max_transport,
        'prepare',
        AsyncMock(return_value=([poll], [])),
    )
    monkeypatch.setattr(
        max_transport,
        'transmit',
        AsyncMock(return_value={'id': '42', 'polls': [poll]}),
    )
    db = NS(
        saved_step=AsyncMock(return_value=None),
        mark_sending=AsyncMock(),
        save_step=AsyncMock(),
        save_links=AsyncMock(),
    )
    tg = NS(call=AsyncMock())
    delivery = Delivery(NS(db=db, tg=tg, media=NS()))
    delivery.interactions = NS(publish_poll=AsyncMock())
    await delivery.send_max(
        NS(client=NS()),
        {'owner': 101, 'max_chat_id': 400},
        {'source_id': '60'},
        {'text': '', 'entities': [], 'attachments': [poll]},
        None,
    )
    tg.call.assert_not_awaited()
    delivery.interactions.publish_poll.assert_not_awaited()
