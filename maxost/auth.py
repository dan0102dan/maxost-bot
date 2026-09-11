from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .errors import BridgeError, Rejected, RetryLater
from .telegram import TelegramRejected
from .ui import Screen, apply_key, card, fallback, valid_callback, validate_phone

log = logging.getLogger(__name__)


class Providers:
    def __init__(self, auth, screen):
        self.auth, self.screen = auth, screen

    async def request(self, phase):
        s = self.screen
        if self.auth.screens.get(s.owner) is not s:
            raise Rejected('Вход отменён.')
        async with s.lock:
            s.phase, s.buffer = phase, ''
            s.future = asyncio.get_running_loop().create_future()
            future = s.future
        await self.auth.render(s)
        try:
            return await asyncio.wait_for(future, max(0.1, s.expires-time.monotonic()))
        finally:
            if s.future is future:
                s.future = None
            s.buffer = ''

    async def get_code(self, phone):
        return await self.request('code')

    async def get_password(self, hint=None):
        # Do not expose a potentially sensitive server-supplied password hint.
        return await self.request('password')


class Authentication:
    def __init__(self, db, tg, hub, settings):
        self.db, self.tg, self.hub, self.settings = db, tg, hub, settings
        self.screens, self.renders, self.render_locks = {}, {}, {}

    async def render(self, s):
        async with self.render_locks.setdefault(s.owner, asyncio.Lock()):
            if self.screens.get(s.owner) is not s:
                return
            rich = card(s)
            params = {'chat_id':s.owner, 'protect_content':True}
            if self.settings.rich_ui:
                params['rich_message'] = rich
                method = 'sendRichMessage' if s.message_id is None else 'editMessageText'
            else:
                text, markup = fallback(rich)
                params.update(text=text, parse_mode='HTML', reply_markup=markup)
                method = 'sendMessage' if s.message_id is None else 'editMessageText'
            if s.message_id is not None:
                params['message_id'] = s.message_id
                params.pop('protect_content', None)
            try:
                result = await self.tg.call(method, params)
            except TelegramRejected as exc:
                if 'message is not modified' in exc.description.lower():
                    return
                raise
            if isinstance(result,dict) and 'message_id' in result:
                s.message_id = result['message_id']

    def refresh(self, s):
        old = self.renders.get(s.owner)
        if old and not old.done():
            # An in-flight render always reads the latest state again below.
            return
        async def redraw():
            try:
                await asyncio.sleep(0.12)
                for _ in range(3):
                    snapshot = (s.phase, s.buffer, s.notice)
                    try:
                        await self.render(s)
                    except RetryLater as exc:
                        await asyncio.sleep(min(exc.delay,10))
                        continue
                    if snapshot == (s.phase,s.buffer,s.notice):
                        break
            except Exception as exc:
                log.warning('Authentication screen update failed (%s)', type(exc).__name__)
        self.renders[s.owner] = asyncio.create_task(redraw())

    async def start(self, owner, disconnect=False):
        existing=self.screens.get(owner)
        if existing and existing.phase=='disconnecting':
            raise Rejected('Отключение уже выполняется.')
        await self.cancel(owner, display=False)
        if not disconnect and await self.db.account(owner):
            raise Rejected('Аккаунт уже подключён. /status — состояние, /disconnect — сменить аккаунт.')
        if len(self.screens) >= self.settings.max_accounts*4:
            raise Rejected('Слишком много одновременных входов. Повторите позже.')
        s = Screen(owner=owner, phase='disconnect' if disconnect else 'consent',
                   expires=time.monotonic()+self.settings.auth_ttl)
        self.screens[owner] = s
        await self.render(s)

    async def cancel(self, owner, display=True):
        s = self.screens.get(owner)
        if not s:
            return
        if s.phase=='disconnecting' and display:
            raise Rejected('Отключение уже началось и не может быть отменено.')
        s.wipe()
        if s.task and not s.task.done():
            s.task.cancel()
            await asyncio.gather(s.task, return_exceptions=True)
        if s.account_id:
            await self.db.pool.execute("DELETE FROM accounts WHERE id=$1 AND owner=$2 AND status='authorizing'", s.account_id,owner)
        s.phase, s.notice = 'cancelled','Вход отменён. Данные ввода удалены.'
        if display:
            with contextlib.suppress(BridgeError):
                await self.render(s)
        self.screens.pop(owner,None)
        old = self.renders.pop(owner,None)
        if old and not old.done():
            old.cancel()
            await asyncio.gather(old,return_exceptions=True)
        self.render_locks.pop(owner,None)

    async def callback(self, query):
        owner = query.get('from',{}).get('id')
        s = self.screens.get(owner)
        if not s:
            return
        async with s.lock:
            key = valid_callback(s, query)
            if key is None:
                return
            if key=='cancel':
                # Cancellation may await a task whose provider also needs s.lock.
                cancel = True
            else:
                cancel = False
                if s.phase=='consent' and key=='accept':
                    await self.db.consent(owner)
                    s.phase='phone'
                elif s.phase=='disconnect' and key=='confirm':
                    s.phase='disconnecting'
                    s.task=asyncio.create_task(self._disconnect(s))
                elif s.phase=='phone' and key=='submit':
                    s.phone = validate_phone(s.buffer)
                    s.buffer, s.phase = '', 'requesting'
                    s.task = asyncio.create_task(self._login(s))
                elif s.phase=='code' and key=='submit':
                    if not s.buffer.isascii() or not s.buffer.isdecimal() or not 4<=len(s.buffer)<=8:
                        raise Rejected('Введите 4–8 цифр кода и нажмите «Войти в MAX».')
                    if s.future and not s.future.done():
                        value, s.buffer, s.phase = s.buffer, '', 'checking'
                        s.future.set_result(value)
                else:
                    apply_key(s,key)
        if cancel:
            await self.cancel(owner)
        else:
            self.refresh(s)

    async def password(self, message):
        s = self.screens.get(message['from']['id'])
        if not s or s.phase!='password':
            return False
        # All messages during this phase are consumed, including topic messages.
        # They must NEVER fall through to the relay.
        value = message.get('text','')
        await self.tg.remove(s.owner,message['message_id'])
        if value.startswith('/'):
            await self.cancel(s.owner)
            return True
        async with s.lock:
            if time.monotonic()>s.expires:
                raise Rejected('Время входа истекло. /connect')
            if not value or len(value)>256:
                raise Rejected('Пароль должен содержать от 1 до 256 символов.')
            if s.future and not s.future.done():
                s.phase='checking'
                s.future.set_result(value)
        self.refresh(s)
        return True

    async def _login(self,s):
        try:
            account = await self.db.new_account(s.owner,s.phone,self.settings.max_accounts)
            s.account_id=account['id']
            async with asyncio.timeout(max(0.1,s.expires-time.monotonic())):
                await self.hub.authenticate(account,Providers(self,s))
            s.phase,s.account_id='ready',None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('MAX authorization failed (%s)',type(exc).__name__)
            s.phase='error'
            s.notice=str(exc) if isinstance(exc,BridgeError) else 'Не удалось войти. Проверьте номер и код; повторите /connect. Если включена 2FA, потребуется её пароль.'
        finally:
            s.wipe()
            if s.account_id:
                await self.db.pool.execute("DELETE FROM accounts WHERE id=$1 AND owner=$2 AND status='authorizing'",s.account_id,s.owner)
                s.account_id=None
            if self.screens.get(s.owner) is s:
                self.refresh(s)

    async def _disconnect(self,s):
        try:
            revoked = await self.hub.disconnect(s.owner)
            s.phase='done'
            s.notice='Данные сервиса удалены; аккаунт отключён.'
            if not revoked:
                s.notice+=' Не удалось подтвердить отзыв сессии на стороне MAX. Завершите её в MAX → Устройства.'
        except Exception as exc:
            log.warning('Disconnect failed (%s)',type(exc).__name__)
            s.phase,s.notice='error','Не удалось завершить отключение. Повторите /disconnect.'
        finally:
            self.refresh(s)

    async def expire(self):
        for owner,s in list(self.screens.items()):
            if time.monotonic()>s.expires:
                await self.cancel(owner,display=s.phase not in ('ready','done'))

    async def close(self):
        for owner in list(self.screens):
            await self.cancel(owner,display=False)
