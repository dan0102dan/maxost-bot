"""Native Telegram polls backed by the original MAX poll."""
from __future__ import annotations

import hashlib
import json

from .errors import Rejected
from .telegram import TelegramRejected


def poll_signature(poll):
    shape = [
        poll.get('poll_id'), poll.get('title'), poll.get('answers'),
        int(poll.get('settings', 0)) & ~8,
    ]
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def native_parameters(poll):
    """Return a Telegram sendPoll payload only when semantics are preserved."""
    title = poll.get('title', '')
    answers = poll.get('answers', [])
    flags = int(poll.get('settings', 0))
    if flags & 16:
        return None
    if not isinstance(title, str) or not 1 <= len(title.encode('utf-16-le')) // 2 <= 300:
        return None
    if not isinstance(answers, list) or not 1 <= len(answers) <= 12:
        return None
    if type(poll.get('poll_id')) is not int or any(not isinstance(a, dict) for a in answers):
        return None
    ids = [a.get('answer_id') for a in answers]
    if any(type(aid) is not int for aid in ids) or len(set(ids)) != len(ids):
        return None
    if any(
        not isinstance(a.get('text'), str)
        or not 1 <= len(a['text'].encode('utf-16-le')) // 2 <= 100
        for a in answers
    ):
        return None
    return {
        'question': title,
        'options': [{'text': a['text']} for a in answers],
        'is_anonymous': False,
        'type': 'regular',
        'allows_multiple_answers': bool(flags & 2),
        'allows_revoting': bool(flags & 4),
        'is_closed': bool(flags & 8),
        'description': 'Ваш выбор отправится в MAX.',
        'protect_content': True,
    }


def results_text(poll):
    state = poll.get('state') or {}
    counts = {
        r['answer_id']: r.get('vote_count', 0)
        for r in state.get('result') or []
    }
    lines = [f"Результаты MAX · {state.get('total', 0)} голосов"]
    for n, answer in enumerate(poll.get('answers', []), 1):
        lines.append(
            f"{n}. {answer['text']} — {counts.get(answer.get('answer_id'), 0)}"
        )
    text = '\n'.join(lines)
    return text if len(text) <= 200 else text[:199] + '…'


def fallback_text(poll):
    kind = 'Викторина MAX' if int(poll.get('settings', 0)) & 16 else 'Опрос MAX'
    lines = [f"{kind}: {poll.get('title') or 'Без названия'}"]
    for n, answer in enumerate(poll.get('answers', []), 1):
        lines.append(f"{n}. {answer.get('text', '')}")
    lines.append('Этот тип опроса нельзя воспроизвести нативно в Telegram.')
    text = '\n'.join(lines)
    return text if len(text) <= 4000 else text[:3999] + '…'


class NativePolls:
    def __init__(self, interactions):
        self.i = interactions
        self.db, self.tg = interactions.db, interactions.tg

    @staticmethod
    def markup(row, poll):
        total = (poll.get('state') or {}).get('total', 0)
        return {
            'inline_keyboard': [[{
                'text': f'Результаты MAX · {total}',
                'callback_data': f"np:{row['id']}:{poll_signature(poll)}",
            }]]
        }

    async def _stop(self, row):
        if not row.get('tg_poll_id') or row.get('closed'):
            return
        try:
            await self.tg.call(
                'stopPoll',
                {'chat_id': row['owner'], 'message_id': row['tg_message_id']},
            )
        except TelegramRejected as exc:
            known = (
                'poll has already been closed',
                'poll is already closed',
                'message to stop poll not found',
            )
            if not any(text in exc.description.lower() for text in known):
                raise
        await self.db.pool.execute(
            'UPDATE poll_mirrors SET closed=true WHERE id=$1 AND owner=$2',
            row['id'], row['owner'],
        )

    async def _relink(self, dialog, previous, result, max_message_id):
        if not previous or not previous['tg_message_id']:
            return
        if previous['tg_message_id'] == result['id']:
            return
        await self.db.pool.execute(
            '''UPDATE message_links SET tg_message_id=$4
            WHERE dialog_id=$1 AND owner=$2 AND max_message_id=$3
            AND tg_message_id=$5''',
            dialog['id'], dialog['owner'], str(max_message_id), result['id'],
            previous['tg_message_id'],
        )
        await self.tg.remove(dialog['owner'], previous['tg_message_id'])

    async def _publish_fallback(self, dialog, max_message_id, poll, thread, reply, previous):
        row = await self.db.put_poll(dialog, max_message_id, poll)
        text = fallback_text(poll)
        params = {
            'chat_id': dialog['owner'],
            'message_thread_id': thread,
            'text': text,
            'protect_content': True,
        }
        if reply:
            params['reply_parameters'] = {
                'message_id': int(reply),
                'allow_sending_without_reply': True,
            }

        if previous and previous['tg_message_id'] and not previous['tg_poll_id']:
            try:
                await self.tg.call(
                    'editMessageText',
                    {
                        'chat_id': dialog['owner'],
                        'message_id': previous['tg_message_id'],
                        'text': text,
                    },
                )
            except TelegramRejected as exc:
                if 'message is not modified' not in exc.description.lower():
                    raise
            message_id = previous['tg_message_id']
        else:
            await self.tg.pace(dialog['owner'])
            sent = await self.tg.call('sendMessage', params)
            message_id = sent['message_id']

        await self.db.pool.execute(
            '''UPDATE poll_mirrors SET tg_message_id=$3,tg_poll_id=NULL,
            signature=$4,closed=$5 WHERE id=$1 AND owner=$2''',
            row['id'], dialog['owner'], message_id, poll_signature(poll),
            bool(int(poll.get('settings', 0)) & 8),
        )
        result = {'id': message_id, 'kind': 'poll'}
        await self._relink(dialog, previous, result, max_message_id)
        return result

    async def publish(self, dialog, max_message_id, poll, thread, reply=None):
        previous = await self.db.pool.fetchrow(
            '''SELECT * FROM poll_mirrors
            WHERE dialog_id=$1 AND owner=$2 AND max_message_id=$3''',
            dialog['id'], dialog['owner'], str(max_message_id),
        )
        params = native_parameters(poll)
        signature = poll_signature(poll)

        if previous and previous['tg_poll_id'] and (
            previous['signature'] != signature or params is None
        ):
            await self._stop(previous)

        if params is None:
            return await self._publish_fallback(
                dialog, max_message_id, poll, thread, reply, previous
            )

        if previous:
            before = self.db.poll_data(previous)
            if poll_signature(before) == signature and '_telegram_options' in before:
                poll.setdefault('_telegram_options', before['_telegram_options'])

        row = await self.db.put_poll(dialog, max_message_id, poll)
        markup = self.markup(row, poll)

        if previous and previous['tg_poll_id'] and previous['signature'] == signature:
            if params['is_closed']:
                await self._stop(previous)
            try:
                await self.tg.call(
                    'editMessageReplyMarkup',
                    {
                        'chat_id': dialog['owner'],
                        'message_id': previous['tg_message_id'],
                        'reply_markup': markup,
                    },
                )
            except TelegramRejected as exc:
                if 'message is not modified' not in exc.description.lower():
                    raise
            await self.db.pool.execute(
                '''UPDATE poll_mirrors SET signature=$3,closed=$4
                WHERE id=$1 AND owner=$2''',
                row['id'], dialog['owner'], signature, params['is_closed'],
            )
            return {'id': previous['tg_message_id'], 'kind': 'poll'}

        await self.tg.pace(dialog['owner'])
        request = {
            **params,
            'chat_id': dialog['owner'],
            'message_thread_id': thread,
            'reply_markup': markup,
        }
        if reply:
            request['reply_parameters'] = {
                'message_id': int(reply),
                'allow_sending_without_reply': True,
            }
        sent = await self.tg.call('sendPoll', request)

        stored = dict(poll)
        stored['_telegram_options'] = [
            option.get('persistent_id') for option in sent['poll'].get('options', [])
        ]
        row = await self.db.put_poll(dialog, max_message_id, stored)
        await self.db.pool.execute(
            '''UPDATE poll_mirrors SET tg_message_id=$3,tg_poll_id=$4,
            signature=$5,closed=$6 WHERE id=$1 AND owner=$2''',
            row['id'], dialog['owner'], sent['message_id'], sent['poll']['id'],
            signature, params['is_closed'],
        )
        result = {'id': sent['message_id'], 'kind': 'poll'}
        await self._relink(dialog, previous, result, max_message_id)
        return result

    async def answer(self, update):
        user = update.get('user') or {}
        owner, poll_id = user.get('id'), update.get('poll_id')
        if (
            type(owner) is not int or owner <= 0 or user.get('is_bot')
            or update.get('voter_chat') or not isinstance(poll_id, str)
        ):
            return
        async with self.i.lock(owner):
            row = await self.db.pool.fetchrow(
                'SELECT * FROM poll_mirrors WHERE owner=$1 AND tg_poll_id=$2',
                owner, poll_id,
            )
            if row is None:
                return
            poll = self.db.poll_data(row)
            if row['closed'] or int(poll.get('settings', 0)) & 8:
                raise Rejected('Опрос закрыт.')
            if row['signature'] != poll_signature(poll):
                raise Rejected('Опрос изменился. Выберите ответ в новом опросе.')

            options = update.get('option_ids', [])
            persistent = update.get('option_persistent_ids')
            known = poll.get('_telegram_options', [])
            if persistent and known and all(known):
                if any(item not in known for item in persistent):
                    raise Rejected('Варианты опроса изменились.')
                options = [known.index(item) for item in persistent]
            if (
                not isinstance(options, list)
                or any(
                    type(index) is not int or not 0 <= index < len(poll['answers'])
                    for index in options
                )
                or len(set(options)) != len(options)
            ):
                raise Rejected('Неизвестный вариант ответа.')

            flags = int(poll.get('settings', 0))
            if len(options) > 1 and not flags & 2:
                raise Rejected('Выберите один ответ.')
            if not options and not flags & 4:
                raise Rejected('В этом опросе нельзя отозвать голос.')

            dialog = await self.db.pool.fetchrow(
                'SELECT * FROM dialogs WHERE id=$1 AND owner=$2',
                row['dialog_id'], owner,
            )
            if dialog is None:
                return
            max_message_id = row['max_message_id']
            await self.db.enqueue(
                dialog,
                'max',
                'vote',
                max_message_id,
                f"telegram:poll:{update['update_id']}",
                [{
                    'max_id': max_message_id,
                    'poll_id': poll['poll_id'],
                    'answer_ids': [
                        poll['answers'][index]['answer_id']
                        for index in sorted(options)
                    ],
                    'native_signature': row['signature'],
                }],
            )

    async def callback(self, query):
        try:
            _, row_id, signature = query.get('data', '').split(':')
            row_id = int(row_id)
            if not 0 < row_id < 2**63:
                raise ValueError()
        except (ValueError, TypeError):
            raise Rejected('Кнопка устарела.') from None

        owner = (query.get('from') or {}).get('id')
        message = query.get('message') or {}
        if (
            message.get('chat', {}).get('id') != owner
            or message.get('chat', {}).get('type') != 'private'
        ):
            raise Rejected('Кнопка недоступна.')

        async with self.i.lock(owner):
            row = await self.db.pool.fetchrow(
                'SELECT * FROM poll_mirrors WHERE id=$1 AND owner=$2',
                row_id, owner,
            )
            if (
                not row or row['tg_message_id'] != message.get('message_id')
                or row['signature'] != signature
            ):
                raise Rejected('Кнопка устарела.')
            dialog = await self.db.pool.fetchrow(
                'SELECT * FROM dialogs WHERE id=$1 AND owner=$2',
                row['dialog_id'], owner,
            )
            if not dialog or dialog['tg_thread_id'] != message.get('message_thread_id'):
                raise Rejected('Откройте опрос в его топике.')
            await self.db.enqueue(
                dialog, 'tg', 'poll_refresh', row['max_message_id'], query['id'], [{}]
            )
            await self.tg.call(
                'answerCallbackQuery',
                {
                    'callback_query_id': query['id'],
                    'text': results_text(self.db.poll_data(row)),
                    'show_alert': True,
                },
            )
