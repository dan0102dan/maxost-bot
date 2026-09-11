"""Native Telegram interactions mirrored to the original MAX objects."""
from __future__ import annotations

import asyncio
import uuid
from weakref import WeakValueDictionary

from .errors import Rejected
from .formatting import value
from .max_client import mutate
from .native_polls import NativePolls, poll_signature
from .native_reactions import mirror_reactions


def reaction_counts(info):
    """Exclude the connected account's own reaction from mirrored counters."""
    mine = value(info, 'your_reaction')
    counters = []
    for counter in value(info, 'counters', []) or []:
        reaction = value(counter, 'reaction')
        count = int(value(counter, 'count', 0)) - int(reaction == mine)
        if count > 0:
            counters.append({'reaction': reaction, 'count': count})
    return counters


class Interactions:
    def __init__(self, bridge):
        self.bridge, self.db, self.tg = bridge, bridge.db, bridge.tg
        self._locks = WeakValueDictionary()
        self.native_polls = NativePolls(self)

    def lock(self, owner):
        lock = self._locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[owner] = lock
        return lock

    async def publish_poll(self, dialog, max_message_id, poll, thread, reply):
        async with self.lock(dialog['owner']):
            return await self.native_polls.publish(
                dialog, max_message_id, dict(poll), thread, reply
            )

    async def schedule(self, entry, chat_id, max_message_id, action):
        dialog = await self.db.pool.fetchrow(
            'SELECT * FROM dialogs WHERE account_id=$1 AND owner=$2 AND max_chat_id=$3',
            entry.account['id'], entry.account['owner'], chat_id,
        )
        if not dialog:
            return
        linked = await self.db.links(dialog, 'max', max_message_id)
        pending = await self.db.pool.fetchval(
            "SELECT 1 FROM jobs WHERE dialog_id=$1 AND owner=$2 "
            "AND direction='tg' AND action='send' AND source_id=$3",
            dialog['id'], dialog['owner'], str(max_message_id),
        )
        if linked or pending:
            await self.db.enqueue(
                dialog, 'tg', action, str(max_message_id), uuid.uuid4().hex, [{}]
            )

    async def native_reaction(self, update):
        owner = (update.get('user') or {}).get('id')
        if (
            not owner or (update.get('user') or {}).get('is_bot')
            or update.get('chat', {}).get('type') != 'private'
            or update['chat'].get('id') != owner
        ):
            return
        rows = await self.db.pool.fetch(
            '''SELECT d.*,l.max_message_id FROM message_links l
            JOIN dialogs d ON d.id=l.dialog_id AND d.owner=l.owner
            WHERE l.owner=$1 AND l.tg_message_id=$2
            ORDER BY l.part LIMIT 1''',
            owner, update['message_id'],
        )
        if not rows:
            return
        dialog = rows[0]
        reactions = update.get('new_reaction', [])
        if len(reactions) > 1 or any(item['type'] != 'emoji' for item in reactions):
            raise Rejected('Выберите одну обычную эмодзи-реакцию.')
        await self.db.enqueue(
            dialog,
            'max',
            'react',
            dialog['max_message_id'],
            str(update['update_id']),
            [{
                'max_id': dialog['max_message_id'],
                'emoji': reactions[0]['emoji'] if reactions else None,
            }],
        )

    async def deliver(self, entry, dialog, job, payload):
        max_message_id = str(payload.get('max_id', job['source_id']))
        links = await self.db.links(dialog, 'max', max_message_id)
        if not links:
            return
        telegram_message_id = links[0]['tg_message_id']

        if job['action'] == 'react':
            async def react():
                if payload.get('emoji'):
                    await mutate(
                        entry.client,
                        'add_reaction',
                        chat_id=dialog['max_chat_id'],
                        message_id=int(max_message_id),
                        reaction=payload['emoji'],
                    )
                else:
                    await mutate(
                        entry.client,
                        'remove_reaction',
                        chat_id=dialog['max_chat_id'],
                        message_id=int(max_message_id),
                    )
                return {'done': True}

            await self.bridge.delivery.step(job, 'react', react)
            return

        if job['action'] == 'reaction':
            info = await entry.client.get_reactions(
                dialog['max_chat_id'], [int(max_message_id)]
            )
            counters = reaction_counts((info or {}).get(max_message_id))
            await self.bridge.delivery.step(
                job,
                'reaction-native',
                lambda: mirror_reactions(
                    self.tg, dialog['owner'], telegram_message_id, counters
                ),
            )
            return

        message = await entry.client.get_message(
            dialog['max_chat_id'], int(max_message_id)
        )
        polls = [
            attachment
            for attachment in getattr(message, 'attaches', [])
            if value(
                value(attachment, 'type'),
                'value',
                value(attachment, 'type'),
            ) == 'POLL'
        ]
        if not polls:
            raise Rejected('Опрос удалён или изменён в MAX.')
        poll = polls[0].model_dump(mode='json')

        if job['action'] == 'vote':
            valid_ids = {answer['answer_id'] for answer in poll['answers']}
            if (
                poll['poll_id'] != payload['poll_id']
                or not set(payload['answer_ids']) <= valid_ids
                or int(poll['settings']) & 8
                or (
                    payload.get('native_signature')
                    and payload['native_signature'] != poll_signature(poll)
                )
            ):
                raise Rejected('Опрос изменился или закрыт.')

            async def vote():
                state = await mutate(
                    entry.client,
                    'vote_poll',
                    chat_id=dialog['max_chat_id'],
                    message_id=int(max_message_id),
                    poll_id=payload['poll_id'],
                    answer_ids=payload['answer_ids'],
                )
                return state.model_dump(mode='json')

            poll['state'] = await self.bridge.delivery.step(job, 'vote', vote)

        await self.bridge.delivery.step(
            job,
            'poll-native',
            lambda: self.publish_poll(
                dialog,
                max_message_id,
                poll,
                dialog['tg_thread_id'],
                telegram_message_id,
            ),
        )
