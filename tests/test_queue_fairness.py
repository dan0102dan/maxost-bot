"""Select jobs by queue age, never by the age of their dialog."""
import base64
import os
import uuid

import pytest

from maxost.crypto import Vault
from maxost.db import Database


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['tg', 'max'])
async def test_old_busy_dialog_does_not_overtake_older_work_in_new_dialog(direction):
    dsn = os.environ.get('TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Requires disposable PostgreSQL')
    import asyncpg

    schema = 'fairness_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA {schema}')
        pool = await asyncpg.create_pool(dsn, min_size=2, max_size=4,
                                        server_settings={'search_path': schema})
        db = Database(pool, Vault(base64.urlsafe_b64encode(os.urandom(32)).decode()))
        await db.migrate()
        await db.consent(101)
        account = await db.new_account(101, '+79990000101', 100)
        await db.activate(account['id'], 101, 1001)
        account = await db.account(101)
        old_dialog = await db.dialog(account, 10, 11, 'Old active dialog')
        new_dialog = await db.dialog(account, 20, 21, 'New dialog')
        await db.pool.execute(
            "UPDATE dialogs SET created_at=now()-interval '1 day' WHERE id=$1",
            old_dialog['id'],
        )
        await db.enqueue(new_dialog, direction, 'send', 'first', '', [{'text': 'first'}])
        for n in range(3):
            await db.enqueue(old_dialog, direction, 'send', f'later-{n}', '', [{'text': 'later'}])
        first = await db.claim()
        assert first['dialog_id'] == new_dialog['id'] and first['source_id'] == 'first'
        await db.complete(first)
        for n in range(3):
            job = await db.claim()
            assert job['source_id'] == f'later-{n}'
            await db.complete(job)
        assert await db.claim() is None
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await admin.close()
