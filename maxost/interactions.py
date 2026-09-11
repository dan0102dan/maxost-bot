"""Owner-scoped reaction and poll cards; votes act on the original MAX object."""
from __future__ import annotations

import asyncio
from weakref import WeakValueDictionary

import hashlib
import json
import uuid

from .errors import Rejected
from .formatting import value
from .max_client import mutate
from .telegram import TelegramRejected

EMOJI = ('👍', '❤️', '🔥', '😂', '👏', '😢', '👎')


def fingerprint(data):
    # Callback indexes must never resolve against a different edited poll.
    content = [data.get('poll_id'), data.get('title'), data.get('answers'), data.get('settings')]
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]


def poll_view(card_id, poll):
    settings = int(poll.get('settings', 0))
    state = poll.get('state', {}) or {}
    counts = {r['answer_id']: r.get('vote_count', 0) for r in state.get('result', []) or []}
    selected = set(poll.get('_selected', []))
    version = fingerprint(poll)
    rows = []
    lines = [('Викторина' if settings & 16 else 'Опрос') + ' MAX', poll['title']]
    for index, answer in enumerate(poll['answers']):
        label = answer['text']
        count = counts.get(answer.get('answer_id'), 0)
        lines.append(f'{index + 1}. {label} — {count}')
        if not settings & 8:
            rows.append([{'text': ('☑ ' if answer.get('answer_id') in selected else '') + label[:80],
                          'callback_data': f'c:{card_id}:{version}:v:{index}'}])
    lines.append(f"Всего голосов в MAX: {state.get('total', 0)}")
    if settings & 8:
        lines.append('Голосование закрыто.')
    else:
        lines.append('Выбор отправляется в MAX от вашего аккаунта.')
        if settings & 2:
            rows.append([{'text': 'Отправить выбор', 'callback_data': f'c:{card_id}:{version}:submit:0'}])
        if settings & 4:
            rows.append([{'text': 'Отозвать голос', 'callback_data': f'c:{card_id}:{version}:clear:0'}])
    rows.append([{'text': 'Обновить результаты', 'callback_data': f'c:{card_id}:{version}:refresh:0'}])
    return '\n'.join(lines), {'inline_keyboard': rows}


def reaction_counts(info):
    """Own reactions are intentionally excluded from mirrored MAX events."""
    mine = value(info, 'your_reaction')
    counters = []
    for counter in value(info, 'counters', []) or []:
        reaction = value(counter, 'reaction')
        count = int(value(counter, 'count', 0)) - int(reaction == mine)
        if count > 0:
            counters.append({'reaction': reaction, 'count': count})
    return counters


def reaction_view(card_id, data):
    text = 'Реакции MAX: ' + (' · '.join(f"{r['reaction']} {r['count']}" for r in data['counters']) or 'нет')
    rows = [[{'text': emoji, 'callback_data': f'c:{card_id}:reaction:r:{n}'} for n, emoji in enumerate(EMOJI[:4])],
            [{'text': emoji, 'callback_data': f'c:{card_id}:reaction:r:{n}'} for n, emoji in enumerate(EMOJI[4:], 4)],
            [{'text': 'Убрать мою реакцию', 'callback_data': f'c:{card_id}:reaction:clear:0'}]]
    return text, {'inline_keyboard': rows}


class Interactions:
    def __init__(self, bridge):
        self.bridge, self.db, self.tg = bridge, bridge.db, bridge.tg
        self._locks = WeakValueDictionary()

    def lock(self, owner):
        lock = self._locks.get(owner)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[owner] = lock
        return lock

    async def publish(self, d, mid, kind, data, thread, reply=None):
        async with self.lock(d['owner']):
            return await self._publish(d, mid, kind, data, thread, reply)

    async def _publish(self, d, mid, kind, data, thread, reply=None):
        previous = await self.db.pool.fetchrow('SELECT * FROM content_cards WHERE dialog_id=$1 AND owner=$2 AND max_message_id=$3 AND kind=$4', d['id'], d['owner'], str(mid), kind)
        if previous and kind == 'reactions' and self.db.card_data(previous) == data and previous['tg_message_id']:
            return {'id': previous['tg_message_id'], 'kind': 'text'}
        if previous and kind == 'poll':
            before = self.db.card_data(previous)
            if fingerprint(before) == fingerprint(data):
                for key in ('_selected', '_seen'):
                    data.setdefault(key, before.get(key, []))
        card = await self.db.put_card(d, mid, kind, data)
        text, markup = poll_view(card['id'], data) if kind == 'poll' else reaction_view(card['id'], data)
        # Telegram's text limit cannot represent an arbitrarily large MAX poll.
        # Explicitly show the overflow instead of silently dropping options.
        if len(text.encode('utf-16-le')) // 2 > 4000:
            raise Rejected('Опрос MAX слишком большой для карточки Telegram. Откройте оригинал в MAX.')
        await self.tg.pace(d['owner'])
        if card['tg_message_id']:
            try:
                await self.tg.call('editMessageText', {'chat_id': d['owner'], 'message_id': card['tg_message_id'], 'text': text, 'reply_markup': markup})
                return {'id': card['tg_message_id'], 'kind': 'poll' if kind == 'poll' else 'text'}
            except TelegramRejected as exc:
                if 'message is not modified' in exc.description.lower():
                    return {'id': card['tg_message_id'], 'kind': 'poll' if kind == 'poll' else 'text'}
                if 'message to edit not found' not in exc.description.lower():
                    raise
        result = await self.tg.call('sendMessage', {'chat_id': d['owner'], 'message_thread_id': thread,
            'text': text, 'reply_markup': markup,
            'reply_parameters': {'message_id': int(reply), 'allow_sending_without_reply': True} if reply else None})
        await self.db.pool.execute('UPDATE content_cards SET tg_message_id=$3 WHERE id=$1 AND owner=$2', card['id'], d['owner'], result['message_id'])
        return {'id': result['message_id'], 'kind': 'poll' if kind == 'poll' else 'text'}

    async def publish_poll(self, d, mid, poll, thread, reply):
        return await self.publish(d, mid, 'poll', dict(poll), thread, reply)

    async def schedule(self, entry, chat_id, mid, action):
        d = await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE account_id=$1 AND owner=$2 AND max_chat_id=$3', entry.account['id'], entry.account['owner'], chat_id)
        if not d:
            return
        linked = await self.db.links(d, 'max', mid)
        pending = await self.db.pool.fetchval("SELECT 1 FROM jobs WHERE dialog_id=$1 AND owner=$2 AND direction='tg' AND action='send' AND source_id=$3", d['id'], d['owner'], str(mid))
        if linked or pending:
            await self.db.enqueue(d, 'tg', action, str(mid), uuid.uuid4().hex, [{}])

    async def target(self, owner, message):
        d = await self.db.by_thread(owner, message.get('message_thread_id'))
        tid = message.get('reply_to_message', {}).get('message_id')
        links = await self.db.links(d, 'tg', tid) if d and tid else []
        if not links:
            raise Rejected('Отправьте команду ответом на сообщение, связанное с MAX, внутри его темы.')
        return d, links[0]['max_message_id']

    async def command(self, owner, message, action):
        d, mid = await self.target(owner, message)
        await self.db.enqueue(d, 'tg', action, mid, uuid.uuid4().hex, [{'force': True}])

    async def callback(self, q):
        async with self.lock(q['from']['id']):
            return await self._callback(q)

    async def _callback(self, q):
        try:
            _, cid, version, action, index = q.get('data', '').split(':')
            cid, index = int(cid), int(index)
            if not 0 < cid < 2**63:
                raise ValueError()
        except (ValueError, TypeError):
            raise Rejected('Некорректная кнопка.') from None
        owner = q['from']['id']
        if q.get('message', {}).get('chat', {}).get('id') != owner or q['message']['chat'].get('type') != 'private':
            raise Rejected('Кнопки доступны только владельцу в личном чате.')
        card = await self.db.pool.fetchrow('SELECT * FROM content_cards WHERE id=$1 AND owner=$2', cid, owner)
        if not card or card['tg_message_id'] != q.get('message', {}).get('message_id'):
            raise Rejected('Карточка устарела или принадлежит другому пользователю.')
        d = await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2', card['dialog_id'], owner)
        if not d or d['tg_thread_id'] != q['message'].get('message_thread_id'):
            raise Rejected('Карточка открыта не в своей теме.')
        data = self.db.card_data(card)
        mid = card['max_message_id']
        if card['kind'] == 'reactions':
            if action == 'r' and 0 <= index < len(EMOJI):
                emoji = EMOJI[index]
            elif action == 'clear':
                emoji = None
            else:
                raise Rejected('Неизвестная реакция.')
            await self.db.enqueue(d, 'max', 'react', mid, q['id'], [{'max_id': mid, 'emoji': emoji}])
            return
        if version != fingerprint(data):
            raise Rejected('Опрос изменился. Используйте обновлённые кнопки.')
        if action == 'refresh':
            await self.db.enqueue(d, 'tg', 'poll_refresh', mid, q['id'], [{}])
            return
        settings = int(data.get('settings', 0))
        if settings & 8:
            raise Rejected('Опрос закрыт.')
        if action == 'v':
            if not 0 <= index < len(data['answers']):
                raise Rejected('Неизвестный вариант ответа.')
            aid = data['answers'][index].get('answer_id')
            if aid is None:
                raise Rejected('MAX не передал идентификатор варианта. Обновите опрос.')
            if settings & 2:
                # Polling is single-consumer; persisted callback IDs prevent a
                # repeated update from toggling the same option twice.
                if q['id'] not in data.get('_seen', []):
                    selected = set(data.get('_selected', []))
                    selected.symmetric_difference_update({aid})
                    data['_selected'] = sorted(selected)
                    data['_seen'] = (data.get('_seen', []) + [q['id']])[-64:]
                await self._publish(d, mid, 'poll', data, d['tg_thread_id'], None)
                return
            answers = [aid]
        elif action == 'submit' and settings & 2:
            answers = data.get('_selected', [])
        elif action == 'clear' and settings & 4:
            answers = []
        else:
            raise Rejected('Недоступное действие опроса.')
        await self.db.enqueue(d, 'max', 'vote', mid, q['id'], [{'max_id': mid, 'poll_id': data['poll_id'], 'answer_ids': answers}])

    async def native_reaction(self, update):
        owner = update.get('user', {}).get('id')
        if not owner or update.get('chat', {}).get('type') != 'private' or update['chat'].get('id') != owner:
            return
        rows = await self.db.pool.fetch('SELECT d.*,l.max_message_id FROM message_links l JOIN dialogs d ON d.id=l.dialog_id AND d.owner=l.owner WHERE l.owner=$1 AND l.tg_message_id=$2 ORDER BY l.part LIMIT 1', owner, update['message_id'])
        if not rows:
            return
        d = rows[0]
        new = update.get('new_reaction', [])
        if len(new) > 1 or any(r['type'] != 'emoji' for r in new):
            raise Rejected('В MAX переносится одна обычная emoji-реакция. Используйте /react ответом на сообщение.')
        await self.db.enqueue(d, 'max', 'react', d['max_message_id'], str(update['update_id']),
            [{'max_id': d['max_message_id'], 'emoji': new[0]['emoji'] if new else None}])

    async def deliver(self, entry, d, job, p):
        mid = str(p.get('max_id', job['source_id']))
        links = await self.db.links(d, 'max', mid)
        if not links:
            return
        reply = links[0]['tg_message_id']
        if job['action'] == 'react':
            async def react():
                if p.get('emoji'):
                    await mutate(entry.client, 'add_reaction', chat_id=d['max_chat_id'], message_id=int(mid), reaction=p['emoji'])
                else:
                    await mutate(entry.client, 'remove_reaction', chat_id=d['max_chat_id'], message_id=int(mid))
                return {'done': True}
            await self.bridge.delivery.step(job, 'react', react)
            return
        if job['action'] == 'reaction':
            info = await entry.client.get_reactions(d['max_chat_id'], [int(mid)])
            counters = reaction_counts((info or {}).get(mid))
            exists = await self.db.pool.fetchval("SELECT 1 FROM content_cards WHERE dialog_id=$1 AND owner=$2 AND kind='reactions' AND max_message_id=$3", d['id'], d['owner'], mid)
            if counters or exists or p.get('force'):
                await self.bridge.delivery.step(job, 'reaction-card', lambda: self.publish(d, mid, 'reactions', {'counters': counters}, d['tg_thread_id'], reply))
            return
        message = await entry.client.get_message(d['max_chat_id'], int(mid))
        polls = [a for a in getattr(message, 'attaches', []) if value(value(a, 'type'), 'value', value(a, 'type')) == 'POLL']
        if not polls:
            raise Rejected('Опрос удалён или изменён в MAX.')
        poll = polls[0].model_dump(mode='json')
        if job['action'] == 'vote':
            valid_ids = {a['answer_id'] for a in poll['answers']}
            if poll['poll_id'] != p['poll_id'] or not set(p['answer_ids']) <= valid_ids or int(poll['settings']) & 8:
                raise Rejected('Опрос изменился или закрыт. Обновите карточку.')
            async def vote():
                state = await mutate(entry.client, 'vote_poll', chat_id=d['max_chat_id'], message_id=int(mid), poll_id=p['poll_id'], answer_ids=p['answer_ids'])
                return state.model_dump(mode='json')
            poll['state'] = await self.bridge.delivery.step(job, 'vote', vote)
            poll['_selected'] = p['answer_ids']
        await self.bridge.delivery.step(job, 'poll-card', lambda: self.publish_poll(d, mid, poll, d['tg_thread_id'], reply))
