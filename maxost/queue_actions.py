"""Owner-bound delivery controls that survive restarts without storing UI sessions."""
from __future__ import annotations

import contextlib
import hmac
import json

from .errors import BridgeError, Rejected
from .telegram import TelegramRejected


def job_version(job):
    """A new attempt or state change invalidates all earlier buttons."""
    return (job['status'], str(job['updated_at']), job['attempts'])


def action_token(vault, job, action):
    data = ['delivery-action', job['owner'], job['id'], *job_version(job), action]
    return vault.digest(json.dumps(data, separators=(',', ':')))[:24]


def error_text(job, confirm=False):
    destination = 'Telegram' if job['direction'] == 'tg' else 'MAX'
    if job['status'] == 'skipped' or job['payload'] is None:
        return f"Сообщение №{job['id']}: срок хранения истёк. Отправьте его заново."
    if confirm:
        return (
            f"Отправить сообщение №{job['id']} ещё раз в {destination}?\n"
            'Оно могло уже дойти. Повтор может создать дубль.'
        )
    if job['status'] == 'unknown':
        return (
            f"Не удалось подтвердить отправку №{job['id']} в {destination}.\n"
            'Проверьте чат: сообщение могло дойти.'
        )
    text = f"Не удалось отправить сообщение №{job['id']} в {destination}."
    # Keep actionable causes (for example /bind), but only in this private notice.
    detail = ' '.join(str(job.get('error') or '').split())
    if detail:
        text += '\n' + (detail if len(detail) <= 240 else detail[:239] + '…')
    return text


class QueueActions:
    def __init__(self, db, tg):
        self.db, self.tg = db, tg

    def markup(self, job, confirm=False):
        if job['status'] not in ('failed', 'unknown'):
            return {'inline_keyboard': []}

        def button(text, action, style):
            return {
                'text': text,
                'style': style,
                'callback_data': (
                    f"dq:{job['id']}:{action}:"
                    f"{action_token(self.db.vault, job, action)}"
                ),
            }

        buttons = []
        if job['payload'] is not None:
            buttons.append(button(
                'Повторить всё равно' if confirm else 'Повторить',
                'confirm' if confirm else 'retry',
                'primary',
            ))
        buttons.append(button('Пропустить', 'skip', 'danger'))
        return {'inline_keyboard': [buttons]}

    async def notify(self, job):
        """Publish a separate error notice, never change the relayed content."""
        params = {
            'chat_id': job['owner'],
            'text': error_text(job),
            'reply_markup': self.markup(job),
            'protect_content': True,
            'link_preview_options': {'is_disabled': True},
        }
        if job['tg_thread_id'] is not None:
            params['message_thread_id'] = job['tg_thread_id']
        # Outbound messages already exist in Telegram. An inbound failure does not.
        source = str(job['source_id'])
        if job['direction'] == 'max' and source.isascii() and source.isdecimal():
            params['reply_parameters'] = {
                'message_id': int(source), 'allow_sending_without_reply': True,
            }
        try:
            await self.tg.pace(job['owner'])
            await self.tg.call('sendMessage', params)
        except TelegramRejected as exc:
            missing_topic = any(word in exc.description.lower() for word in (
                'message thread not found', 'topic_closed', 'topic is closed',
            ))
            if not missing_topic or 'message_thread_id' not in params:
                raise
            params.pop('message_thread_id')
            params.pop('reply_parameters', None)
            await self.tg.pace(job['owner'])
            await self.tg.call('sendMessage', params)
        # A slow notification must not acknowledge a newer failure of this job.
        await self.db.pool.execute(
            '''UPDATE jobs SET notified=true WHERE id=$1 AND owner=$2
            AND status=$3 AND updated_at=$4 AND attempts=$5''',
            job['id'], job['owner'], job['status'], job['updated_at'], job['attempts'],
        )

    async def callback(self, query):
        user = query.get('from') or {}
        owner = user.get('id')
        message = query.get('message') or {}
        chat = message.get('chat') or {}
        if (type(owner) is not int or owner <= 0 or user.get('is_bot')
                or chat.get('type') != 'private' or chat.get('id') != owner
                or type(message.get('message_id')) is not int):
            raise Rejected('Кнопка недоступна.')
        try:
            prefix, raw_id, action, token = query.get('data', '').split(':')
            job_id = int(raw_id)
            if (prefix != 'dq' or not 0 < job_id < 2**63
                    or action not in ('retry', 'skip', 'confirm')
                    or len(token) != 24 or not token.isascii()):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise Rejected('Кнопка недоступна.') from None

        job = await self.db.pool.fetchrow(
            'SELECT * FROM jobs WHERE id=$1 AND owner=$2', job_id, owner,
        )
        if (not job or job['status'] not in ('failed', 'unknown')
                or not hmac.compare_digest(token, action_token(self.db.vault, job, action))):
            raise Rejected('Это действие уже недоступно.')

        confirming = action == 'retry' and job['status'] == 'unknown'
        if confirming:
            text, markup = error_text(job, confirm=True), self.markup(job, confirm=True)
        else:
            if action == 'confirm' and job['status'] != 'unknown':
                raise Rejected('Это действие уже недоступно.')
            operation = 'skip' if action == 'skip' else 'retry'
            await self.db.queue_control(
                owner, job_id, operation, confirm=action == 'confirm',
                expected=job_version(job),
            )
            text = (
                f"Сообщение №{job_id} пропущено."
                if operation == 'skip' else f"Повторная отправка №{job_id} в очереди."
            )
            markup = {'inline_keyboard': []}

        # State was already committed. UI delivery failure must never retry it.
        with contextlib.suppress(BridgeError):
            await self.tg.call('answerCallbackQuery', {'callback_query_id': query['id']})
        with contextlib.suppress(BridgeError):
            await self.tg.call('editMessageText', {
                'chat_id': owner, 'message_id': message['message_id'],
                'text': text, 'reply_markup': markup,
            })
