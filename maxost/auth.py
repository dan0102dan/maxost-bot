from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from io import BytesIO
from urllib.parse import urlparse

from .auth_diagnostics import STAGES, describe_failure, safe_error_code
from .errors import BridgeError, Rejected, RetryLater
from .telegram import TelegramRejected
from .ui import Screen, apply_key, card, fallback, valid_callback, validate_phone

log = logging.getLogger(__name__)


class SwitchToQr(Exception):
    """Internal signal: abandon the SMS transport and reuse this login attempt as QR."""


class Providers:
    def __init__(self, auth, screen, client_kind='mobile'):
        self.auth, self.screen = auth, screen
        self.client_kind = client_kind
        self.input_ready = asyncio.Event()

    async def progress(self, stage, notice=None):
        s = self.screen
        if self.auth.screens.get(s.owner) is not s:
            raise Rejected('Вход отменён.')
        if stage not in STAGES:
            raise ValueError('Unknown authentication stage')
        s.auth_stage = stage
        s.notice = notice or STAGES[stage]
        log.info('MAX auth attempt=%s stage=%s', s.trace_id, stage)
        self.auth.refresh(s)

    async def code_requested(self, length):
        self.screen.code_length = length
        await self.progress('waiting_code')

    async def show_qr(self, qr_url):
        await self.auth.show_qr(self.screen, qr_url)
        self.input_ready.set()

    async def qr_confirmed(self):
        await self.auth.clear_qr(self.screen)

    async def request(self, phase):
        s = self.screen
        if self.auth.screens.get(s.owner) is not s:
            raise Rejected('Вход отменён.')
        async with s.lock:
            s.phase, s.buffer = phase, ''
            s.auth_stage = 'waiting_code' if phase == 'code' else 'waiting_password'
            s.future = asyncio.get_running_loop().create_future()
            future = s.future
        try:
            await self.auth.render(s)
            self.input_ready.set()
            return await asyncio.wait_for(
                future, max(0.1, s.expires-time.monotonic())
            )
        finally:
            if not future.done():
                future.cancel()
            if s.future is future:
                s.future = None
            s.buffer = ''

    async def get_code(self, phone):
        return await self.request('code')

    async def get_password(self, hint=None):
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
                log.warning(
                    'Authentication screen update failed (%s)',
                    type(exc).__name__,
                )
        self.renders[s.owner] = asyncio.create_task(redraw())

    async def show_qr(self, s, qr_url):
        if self.screens.get(s.owner) is not s:
            raise Rejected('Вход отменён.')
        if not isinstance(qr_url, str) or not qr_url or len(qr_url) > 4096:
            raise ValueError('Unsupported QR URL')

        await self.clear_qr(s)

        import qrcode

        qr = qrcode.QRCode(border=4, box_size=8)
        qr.add_data(qr_url)
        qr.make(fit=True)
        image = qr.make_image()
        output = BytesIO()
        image.save(output, format='PNG')
        png = output.getvalue()

        parsed = urlparse(qr_url)
        markup = None
        if parsed.scheme in ('http', 'https') and parsed.netloc:
            markup = {
                'inline_keyboard': [[
                    {'text': 'Открыть ссылку входа в MAX', 'url': qr_url}
                ]]
            }

        result = await self.tg.photo(
            s.owner,
            png,
            caption=(
                'Вход в MAX по QR. Отсканируйте код в официальном приложении '
                'и подтвердите новое устройство. QR действует ограниченное время.'
            ),
            reply_markup=markup,
        )
        if isinstance(result, dict):
            s.qr_message_id = result.get('message_id')
        s.phase = 'qr'
        s.notice = 'Ожидаем подтверждение в MAX…'
        await self.render(s)

    async def clear_qr(self, s):
        message_id = s.qr_message_id
        s.qr_message_id = None
        if message_id:
            await self.tg.remove(s.owner, message_id)

    async def start(self, owner, disconnect=False):
        existing=self.screens.get(owner)
        if existing and existing.phase=='disconnecting':
            raise Rejected('Отключение уже выполняется.')
        await self.cancel(owner, display=False)
        if not disconnect and await self.db.account(owner):
            raise Rejected(
                'Аккаунт уже подключён. /status — состояние, '
                '/disconnect — сменить аккаунт.'
            )
        if len(self.screens) >= self.settings.max_accounts*4:
            raise Rejected(
                'Слишком много одновременных входов. Повторите позже.'
            )
        s = Screen(
            owner=owner,
            phase='disconnect' if disconnect else 'consent',
            expires=time.monotonic()+self.settings.auth_ttl,
        )
        self.screens[owner] = s
        await self.render(s)

    async def cancel(self, owner, display=True):
        s = self.screens.get(owner)
        if not s:
            return
        if s.phase=='disconnecting' and display:
            raise Rejected(
                'Отключение уже началось и не может быть отменено.'
            )
        s.wipe()
        if s.task and not s.task.done():
            s.task.cancel()
            await asyncio.gather(s.task, return_exceptions=True)
        await self.clear_qr(s)
        if s.account_id:
            await self.db.pool.execute(
                "DELETE FROM accounts WHERE id=$1 AND owner=$2 "
                "AND status='authorizing'",
                s.account_id,
                owner,
            )
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
                cancel = True
            else:
                cancel = False
                if s.phase=='consent' and key=='accept':
                    await self.db.consent(owner)
                    s.phase='phone'
                elif s.phase=='disconnect' and key=='confirm':
                    s.phase='disconnecting'
                    s.task=asyncio.create_task(self._disconnect(s))
                elif s.phase=='phone' and key in ('submit','qr'):
                    s.phone = validate_phone(s.buffer)
                    s.buffer, s.phase = '', 'requesting'
                    s.auth_stage, s.notice = 'connecting', STAGES['connecting']
                    s.trace_id = secrets.token_hex(4)
                    kind = 'web' if key == 'qr' else 'mobile'
                    s.task = asyncio.create_task(self._login(s, kind))
                elif s.phase=='code' and key=='submit':
                    length_ok = (
                        len(s.buffer) == s.code_length
                        if s.code_length
                        else 4 <= len(s.buffer) <= 8
                    )
                    if (
                        not s.buffer.isascii()
                        or not s.buffer.isdecimal()
                        or not length_ok
                    ):
                        expected = str(s.code_length) if s.code_length else '4–8'
                        raise Rejected(
                            f'Введите {expected} цифр кода и нажмите «Войти в MAX».'
                        )
                    if s.future and not s.future.done():
                        value, s.buffer, s.phase = s.buffer, '', 'checking'
                        s.future.set_result(value)
                elif s.phase=='code' and key=='qr':
                    if not s.future or s.future.done():
                        raise Rejected(
                            'Текущая попытка уже завершилась. Начните заново: /connect.'
                        )
                    s.buffer, s.phase = '', 'requesting'
                    s.auth_stage = 'requesting_qr'
                    s.notice = 'Переключаем вход на QR…'
                    s.future.set_exception(SwitchToQr())
                else:
                    apply_key(s,key)
        if cancel:
            await self.cancel(owner)
        else:
            self.refresh(s)

    async def password(self, message):
        s = self.screens.get(message['from']['id'])
        if not s:
            return False
        if s.phase == 'qr':
            await self.tg.remove(s.owner, message['message_id'])
            return True
        if s.phase != 'password':
            return False
        value = message.get('text','')
        await self.tg.remove(s.owner,message['message_id'])
        if value.startswith('/'):
            await self.cancel(s.owner)
            return True
        async with s.lock:
            if time.monotonic()>s.expires:
                raise Rejected('Время входа истекло. /connect')
            if not value or len(value)>256:
                raise Rejected(
                    'Пароль должен содержать от 1 до 256 символов.'
                )
            if s.future and not s.future.done():
                s.phase='checking'
                s.future.set_result(value)
        self.refresh(s)
        return True

    async def _run_attempt(self, account, provider, kind):
        provider.client_kind = kind
        login_task = asyncio.create_task(self.hub.authenticate(account, provider))
        waiter = asyncio.create_task(provider.input_ready.wait())
        try:
            done, _ = await asyncio.wait(
                (login_task, waiter),
                timeout=45,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError('MAX initial connection timed out')
            return await login_task
        finally:
            if not waiter.done():
                waiter.cancel()
            if not login_task.done():
                login_task.cancel()
            await asyncio.gather(waiter, login_task, return_exceptions=True)

    async def _login(self, s, kind='mobile'):
        try:
            log.info('MAX auth attempt=%s stage=account_setup', s.trace_id)
            account = await self.db.new_account(
                s.owner,s.phone,self.settings.max_accounts
            )
            s.account_id=account['id']
            async with asyncio.timeout(
                max(0.1,s.expires-time.monotonic())
            ):
                while True:
                    provider = Providers(self,s,kind)
                    await provider.progress('connecting')
                    try:
                        await self._run_attempt(account, provider, kind)
                        break
                    except SwitchToQr:
                        kind = 'web'
                        await self.clear_qr(s)
                        s.phase = 'requesting'
                        s.auth_stage = 'requesting_qr'
                        s.notice = 'SMS-вход остановлен. Создаём QR…'
                        self.refresh(s)
                        continue
            s.phase,s.account_id='ready',None
            log.info(
                'MAX auth attempt=%s stage=ready client=%s',
                s.trace_id,
                kind,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category, description = describe_failure(exc, s.auth_stage)
            code = safe_error_code(exc)
            log.warning(
                'MAX auth attempt=%s stage=%s category=%s code=%s',
                s.trace_id,
                s.auth_stage,
                category,
                code,
            )
            s.phase='error'
            s.notice = str(exc) if isinstance(exc,BridgeError) else description
            s.notice += (
                f'\nДиагностика: {category} / {code}. '
                f'Попытка: {s.trace_id}. /connect — начать заново.'
            )
        finally:
            await self.clear_qr(s)
            s.wipe()
            try:
                if s.account_id:
                    await self.db.pool.execute(
                        "DELETE FROM accounts WHERE id=$1 AND owner=$2 "
                        "AND status='authorizing'",
                        s.account_id,
                        s.owner,
                    )
                    s.account_id=None
            except Exception:
                log.warning(
                    'MAX auth attempt=%s stage=cleanup category=DATABASE',
                    s.trace_id,
                )
            if self.screens.get(s.owner) is s:
                self.refresh(s)

    async def _disconnect(self,s):
        try:
            revoked = await self.hub.disconnect(s.owner)
            s.phase='done'
            s.notice='Данные сервиса удалены; аккаунт отключён.'
            if not revoked:
                s.notice += (
                    ' Не удалось подтвердить отзыв сессии на стороне MAX. '
                    'Завершите её в MAX → Устройства.'
                )
        except Exception as exc:
            log.warning('Disconnect failed (%s)',type(exc).__name__)
            s.phase,s.notice=(
                'error',
                'Не удалось завершить отключение. Повторите /disconnect.',
            )
        finally:
            self.refresh(s)

    async def expire(self):
        for owner,s in list(self.screens.items()):
            if time.monotonic()>s.expires:
                await self.cancel(
                    owner,display=s.phase not in ('ready','done')
                )

    async def close(self):
        for owner in list(self.screens):
            await self.cancel(owner,display=False)
