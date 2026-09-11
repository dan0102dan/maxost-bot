"""Native Telegram polls backed by the original MAX poll, not copied vote totals.

Only polls sent by this bot yield poll_answer updates. The local poll is private
and non-anonymous so its owner's selection can be delivered to MAX. Telegram's
counters are local; the results button shows the authoritative MAX snapshot.
"""
from __future__ import annotations

import hashlib
import json

from .errors import Rejected
from .telegram import TelegramRejected


def poll_signature(poll):
    # Closing a poll is a state transition, not a change to its answer mapping.
    shape = [poll.get('poll_id'), poll.get('title'), poll.get('answers'),
             int(poll.get('settings', 0)) & ~8]
    return hashlib.sha256(json.dumps(shape, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def native_parameters(poll):
    """Return only faithfully representable poll types; never invent quiz answers."""
    title = poll.get('title', '')
    answers = poll.get('answers', [])
    flags = int(poll.get('settings', 0))
    if (flags & 16 or not isinstance(title, str) or not 1 <= len(title.encode('utf-16-le')) // 2 <= 300
            or not isinstance(answers, list) or not 1 <= len(answers) <= 12):
        return None
    if type(poll.get('poll_id')) is not int or any(not isinstance(a, dict) for a in answers):
        return None
    ids = [a.get('answer_id') for a in answers]
    if any(type(aid) is not int for aid in ids) or len(set(ids)) != len(ids):
        return None
    if any(not isinstance(a.get('text'), str) or not 1 <= len(a['text'].encode('utf-16-le')) // 2 <= 100 for a in answers):
        return None
    return {
        'question': title,
        'options': [{'text': a['text']} for a in answers],
        'is_anonymous': False,
        'type': 'regular',
        'allows_multiple_answers': bool(flags & 2),
        'allows_revoting': bool(flags & 4),
        'is_closed': bool(flags & 8),
        'description': 'Ваш выбор отправится в MAX. Общие результаты — по кнопке ниже.',
        'protect_content': True,
    }


def results_text(poll):
    state = poll.get('state') or {}
    counts = {r['answer_id']: r.get('vote_count', 0) for r in state.get('result') or []}
    lines = [f"Результаты MAX · {state.get('total', 0)} голосов"]
    for n, a in enumerate(poll.get('answers', []), 1):
        lines.append(f"{n}. {a['text']} — {counts.get(a.get('answer_id'), 0)}")
    # Callback alerts have a 200-character limit. The source poll stays intact.
    text = '\n'.join(lines)
    return text if len(text) <= 200 else text[:199] + '…'


class NativePolls:
    def __init__(self, interactions):
        self.i = interactions
        self.db, self.tg = interactions.db, interactions.tg

    @staticmethod
    def markup(card, poll):
        total = (poll.get('state') or {}).get('total', 0)
        return {'inline_keyboard': [[{
            'text': f'Результаты MAX · {total}',
            'callback_data': f"np:{card['id']}:{poll_signature(poll)}",
        }]]}

    async def _stop(self, card):
        if not card.get('tg_poll_id') or card.get('poll_closed'):
            return
        try:
            await self.tg.call('stopPoll', {'chat_id': card['owner'], 'message_id': card['tg_message_id']})
        except TelegramRejected as exc:
            # Do not turn permission or server errors into a false success.
            if not any(t in exc.description.lower() for t in ('poll has already been closed', 'poll is already closed', 'message to stop poll not found')):
                raise
        await self.db.pool.execute(
            'UPDATE content_cards SET poll_closed=true WHERE id=$1 AND owner=$2', card['id'], card['owner'])

    async def _relink(self, d, previous, result, mid):
        if previous and previous['tg_message_id'] and previous['tg_message_id'] != result['id']:
            await self.db.pool.execute('''UPDATE message_links SET tg_message_id=$4
                WHERE dialog_id=$1 AND owner=$2 AND max_message_id=$3 AND tg_message_id=$5''',
                d['id'], d['owner'], str(mid), result['id'], previous['tg_message_id'])
            # Older poll votes must never be applied to the replacement's options.
            await self.tg.remove(d['owner'], previous['tg_message_id'])

    async def publish(self, d, mid, poll, thread, reply=None):
        """Called under Interactions.lock(owner), including fallback publication."""
        previous = await self.db.pool.fetchrow('''SELECT * FROM content_cards
            WHERE dialog_id=$1 AND owner=$2 AND max_message_id=$3 AND kind='poll' ''',
            d['id'], d['owner'], str(mid))
        params = native_parameters(poll)
        signature = poll_signature(poll)
        if previous and previous.get('tg_poll_id') and (
                previous.get('poll_signature') != signature or params is None):
            await self._stop(previous)
        if params is None:
            if previous and previous.get('tg_poll_id'):
                # A native poll cannot become a text card through editMessageText.
                await self.db.pool.execute('''UPDATE content_cards SET tg_message_id=NULL,
                    tg_poll_id=NULL,poll_signature=NULL,poll_closed=false WHERE id=$1 AND owner=$2''',
                    previous['id'], d['owner'])
            result = await self.i._publish(d, mid, 'poll', dict(poll), thread, reply)
            if previous and previous.get('tg_poll_id'):
                await self._relink(d, previous, result, mid)
            return result
        if previous:
            before = self.db.card_data(previous)
            if poll_signature(before) == signature:
                for key in ('_selected', '_seen', '_telegram_options'):
                    if key in before:
                        poll.setdefault(key, before[key])
        card = await self.db.put_card(d, mid, 'poll', poll)
        markup = self.markup(card, poll)
        if previous and previous.get('tg_poll_id') and previous.get('poll_signature') == signature:
            if params['is_closed']:
                await self._stop(previous)
            try:
                await self.tg.call('editMessageReplyMarkup', {
                    'chat_id': d['owner'], 'message_id': previous['tg_message_id'], 'reply_markup': markup})
            except TelegramRejected as exc:
                if 'message is not modified' not in exc.description.lower():
                    raise
            return {'id': previous['tg_message_id'], 'kind': 'poll'}
        await self.tg.pace(d['owner'])
        sent = await self.tg.call('sendPoll', {
            **params, 'chat_id': d['owner'], 'message_thread_id': thread, 'reply_markup': markup,
            'reply_parameters': {'message_id': int(reply), 'allow_sending_without_reply': True} if reply else None})
        # Persist persistent option IDs when supplied by newer Telegram versions.
        local = dict(poll)
        local['_telegram_options'] = [a.get('persistent_id') for a in sent['poll'].get('options', [])]
        card = await self.db.put_card(d, mid, 'poll', local)
        await self.db.pool.execute('''UPDATE content_cards SET tg_message_id=$3,tg_poll_id=$4,
            poll_signature=$5,poll_closed=$6 WHERE id=$1 AND owner=$2''',
            card['id'], d['owner'], sent['message_id'], sent['poll']['id'], signature, params['is_closed'])
        result = {'id': sent['message_id'], 'kind': 'poll'}
        await self._relink(d, previous, result, mid)
        return result

    async def answer(self, update):
        user = update.get('user') or {}
        owner, poll_id = user.get('id'), update.get('poll_id')
        if type(owner) is not int or owner <= 0 or user.get('is_bot') or update.get('voter_chat') or not isinstance(poll_id, str):
            return
        async with self.i.lock(owner):
            card = await self.db.pool.fetchrow('SELECT * FROM content_cards WHERE owner=$1 AND tg_poll_id=$2', owner, poll_id)
            if card is None:
                return  # Foreign, forwarded, replaced, or unknown poll.
            poll = self.db.card_data(card)
            if card.get('poll_closed') or int(poll.get('settings', 0)) & 8:
                raise Rejected('Опрос закрыт.')
            if card.get('poll_signature') != poll_signature(poll):
                raise Rejected('Опрос изменился. Выберите ответ в новом опросе.')
            options = update.get('option_ids', [])
            persistent = update.get('option_persistent_ids')
            known = poll.get('_telegram_options', [])
            if persistent and known and all(known):
                if any(p not in known for p in persistent):
                    raise Rejected('Варианты опроса изменились.')
                options = [known.index(p) for p in persistent]
            if (not isinstance(options, list) or any(type(x) is not int or not 0 <= x < len(poll['answers']) for x in options)
                    or len(set(options)) != len(options)):
                raise Rejected('Неизвестный вариант ответа.')
            flags = int(poll.get('settings', 0))
            if len(options) > 1 and not flags & 2:
                raise Rejected('Выберите один ответ.')
            if not options and not flags & 4:
                raise Rejected('В этом опросе нельзя отозвать голос.')
            d = await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2', card['dialog_id'], owner)
            if d is None:
                return
            mid = card['max_message_id']
            await self.db.enqueue(d, 'max', 'vote', mid, f"telegram:poll:{update['update_id']}", [{
                'max_id': mid, 'poll_id': poll['poll_id'],
                'answer_ids': [poll['answers'][x]['answer_id'] for x in sorted(options)],
                'native_signature': card['poll_signature'],
            }])

    async def callback(self, q):
        """Show the cached MAX results; enqueue a durable refresh for the next tap."""
        try:
            _, cid, signature = q.get('data', '').split(':')
            cid = int(cid)
            if not 0 < cid < 2**63:
                raise ValueError()
        except (ValueError, TypeError):
            raise Rejected('Кнопка устарела.') from None
        owner = (q.get('from') or {}).get('id')
        message = q.get('message') or {}
        if message.get('chat', {}).get('id') != owner or message.get('chat', {}).get('type') != 'private':
            raise Rejected('Кнопка недоступна.')
        async with self.i.lock(owner):
            card = await self.db.pool.fetchrow('SELECT * FROM content_cards WHERE id=$1 AND owner=$2', cid, owner)
            if not card or card['tg_message_id'] != message.get('message_id') or card.get('poll_signature') != signature:
                raise Rejected('Кнопка устарела.')
            d = await self.db.pool.fetchrow('SELECT * FROM dialogs WHERE id=$1 AND owner=$2', card['dialog_id'], owner)
            if not d or d['tg_thread_id'] != message.get('message_thread_id'):
                raise Rejected('Откройте опрос в его топике.')
            await self.db.enqueue(d, 'tg', 'poll_refresh', card['max_message_id'], q['id'], [{}])
            await self.tg.call('answerCallbackQuery', {
                'callback_query_id': q['id'], 'text': results_text(self.db.card_data(card)), 'show_alert': True})
