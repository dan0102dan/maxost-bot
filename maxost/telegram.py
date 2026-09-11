from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict

import httpx

from .errors import Rejected, RetryLater, Uncertain


class TelegramRejected(Rejected):
    def __init__(self, code: int, description: str):
        super().__init__(f"Telegram отклонил запрос (код {code}).")
        self.code, self.description = code, description


READ_METHODS = {'getMe','getUpdates','getFile','getChat','getWebhookInfo'}
IDEMPOTENT = READ_METHODS | {'editMessageText','editMessageCaption','editMessageMedia','editMessageReplyMarkup','setMessageReaction','deleteMessage','answerCallbackQuery','reopenForumTopic','setMyCommands'}


class Telegram:
    def __init__(self, token, base='https://api.telegram.org', client=None):
        self.base, self.token = base.rstrip('/'), token
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(65, connect=15), follow_redirects=False)
        self.send_locks = defaultdict(asyncio.Lock)
        self.last_send = {}

    async def close(self):
        await self.http.aclose()

    async def call(self, method, params=None, files=None):
        params = {k:v for k,v in (params or {}).items() if v is not None}
        try:
            if files:
                data = {k: json.dumps(v,ensure_ascii=False) if not isinstance(v,str) else v for k,v in params.items()}
                response = await self.http.post(f'{self.base}/bot{self.token}/{method}', data=data, files=files)
            else:
                response = await self.http.post(f'{self.base}/bot{self.token}/{method}', json=params)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise RetryLater("Telegram временно недоступен", 5) from None
        except httpx.HTTPError:
            if method in IDEMPOTENT:
                raise RetryLater("Соединение с Telegram прервалось", 5) from None
            raise Uncertain("Ответ Telegram потерян. Отправка могла состояться.") from None
        try:
            body = response.json()
        except ValueError:
            if method in IDEMPOTENT:
                raise RetryLater("Некорректный ответ Telegram", 10) from None
            raise Uncertain("Не удалось определить результат запроса Telegram.") from None
        if body.get('ok'):
            return body['result']
        code = body.get('error_code', response.status_code)
        if code == 429:
            raise RetryLater("Telegram ограничил частоту запросов", body.get('parameters',{}).get('retry_after',5))
        if code >= 500:
            if method in IDEMPOTENT:
                raise RetryLater("Временная ошибка Telegram", 10)
            raise Uncertain("Telegram вернул серверную ошибку; результат отправки неизвестен.")
        raise TelegramRejected(code, body.get('description',''))

    async def pace(self, chat):
        async with self.send_locks[chat]:
            delay = self.last_send.get(chat,0) + 1.05 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_send[chat] = time.monotonic()
        if len(self.last_send) > 10000:
            cutoff = time.monotonic()-300
            for k in list(self.last_send):
                if self.last_send[k] < cutoff and not self.send_locks[k].locked():
                    self.last_send.pop(k,None)
                    self.send_locks.pop(k,None)

    async def text(self, owner, text, thread=None, reply=None):
        await self.pace(owner)
        return await self.call('sendMessage', {
            'chat_id':owner, 'message_thread_id':thread, 'text':text,
            'reply_parameters':{'message_id':reply,'allow_sending_without_reply':True} if reply else None,
            'link_preview_options':{'is_disabled':True},
        })

    async def remove(self, owner, message_id):
        try:
            await self.call('deleteMessage', {'chat_id':owner,'message_id':message_id})
            return True
        except (TelegramRejected, RetryLater):
            return False

    async def download(self, file_id, limit):
        info = await self.call('getFile', {'file_id':file_id})
        path = info.get('file_path')
        if not path or path.startswith('/') or '..' in path.split('/'):
            raise Rejected("Недопустимый путь файла Telegram.")
        if info.get('file_size',0) > limit:
            raise Rejected("Вложение превышает лимит размера.")
        try:
            async with self.http.stream('GET', f'{self.base}/file/bot{self.token}/{path}') as r:
                r.raise_for_status()
                chunks, length = [], 0
                async for chunk in r.aiter_bytes():
                    length += len(chunk)
                    if length > limit:
                        raise Rejected("Вложение превышает лимит размера.")
                    chunks.append(chunk)
                return b''.join(chunks)
        except httpx.HTTPError:
            raise RetryLater("Не удалось загрузить файл из Telegram", 10) from None
