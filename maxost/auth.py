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
from .ui import Screen, apply_key, card, valid_callback, validate_phone

log = logging.getLogger(__name__)


class SwitchToQr(Exception):
    """Switch the current explicit login attempt from SMS to QR."""


class Providers:
    def __init__(self, auth, screen, client_kind='mobile'):
        self.auth, self.screen = auth, screen
        self.client_kind = client_kind
        self.input_ready = asyncio.Event()

    async def progress(self, stage, notice=None):
        screen = self.screen
        if self.auth.screens.get(screen.owner) is not screen:
            raise Rejected('Вход отменён.')
        if stage not in STAGES:
            raise ValueError('Unknown authentication stage')
        screen.auth_stage = stage
        screen.notice = notice or STAGES[stage]
        log.info('MAX auth attempt=%s stage=%s', screen.trace_id, stage)
        self.auth.refresh(screen)

    async def code_requested(self, length):
        self.screen.code_length = length
        await self.progress('waiting_code')

    async def show_qr(self, qr_url):
        await self.auth.show_qr(self.screen, qr_url)
        self.input_ready.set()

    async def qr_confirmed(self):
        await self.auth.clear_qr(self.screen)

    async def request(self, phase):
        screen = self.screen
        if self.auth.screens.get(screen.owner) is not screen:
            raise Rejected('Вход отменён.')
        async with screen.lock:
            screen.phase, screen.buffer = phase, ''
            screen.auth_stage = (
                'waiting_code' if phase == 'code' else 'waiting_password'
            )
            screen.future = asyncio.get_running_loop().create_future()
            future = screen.future
        try:
            await self.auth.render(screen)
            self.input_ready.set()
            return await asyncio.wait_for(
                future,
                max(0.1, screen.expires - time.monotonic()),
            )
        finally:
            if not future.done():
                future.cancel()
            if screen.future is future:
                screen.future = None
            screen.buffer = ''

    async def get_code(self, phone):
        return await self.request('code')

    async def get_password(self, hint=None):
        return await self.request('password')


class Authentication:
    def __init__(self, db, tg, hub, settings):
        self.db, self.tg, self.hub, self.settings = db, tg, hub, settings
        self.screens, self.renders, self.render_locks = {}, {}, {}

    async def render(self, screen):
        async with self.render_locks.setdefault(screen.owner, asyncio.Lock()):
            if self.screens.get(screen.owner) is not screen:
                return
            params = {
                'chat_id': screen.owner,
                'rich_message': card(screen),
                'protect_content': True,
            }
            method = (
                'sendRichMessage'
                if screen.message_id is None
                else 'editMessageText'
            )
            if screen.message_id is not None:
                params['message_id'] = screen.message_id
                params.pop('protect_content', None)
            try:
                result = await self.tg.call(method, params)
            except TelegramRejected as exc:
                if 'message is not modified' in exc.description.lower():
                    return
                raise
            if isinstance(result, dict) and 'message_id' in result:
                screen.message_id = result['message_id']

    def refresh(self, screen):
        old = self.renders.get(screen.owner)
        if old and not old.done():
            return

        async def redraw():
            try:
                await asyncio.sleep(0.12)
                for _ in range(3):
                    snapshot = (screen.phase, screen.buffer, screen.notice)
                    try:
                        await self.render(screen)
                    except RetryLater as exc:
                        await asyncio.sleep(min(exc.delay, 10))
                        continue
                    if snapshot == (
                        screen.phase,
                        screen.buffer,
                        screen.notice,
                    ):
                        break
            except Exception as exc:
                log.warning(
                    'Authentication screen update failed (%s)',
                    type(exc).__name__,
                )

        self.renders[screen.owner] = asyncio.create_task(redraw())

    async def show_qr(self, screen, qr_url):
        if self.screens.get(screen.owner) is not screen:
            raise Rejected('Вход отменён.')
        if not isinstance(qr_url, str) or not qr_url or len(qr_url) > 4096:
            raise ValueError('Unsupported QR URL')

        await self.clear_qr(screen)

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
                    {'text': 'Открыть в MAX', 'url': qr_url}
                ]]
            }

        result = await self.tg.photo(
            screen.owner,
            png,
            caption='Отсканируйте QR в MAX и подтвердите вход.',
            reply_markup=markup,
        )
        if isinstance(result, dict):
            screen.qr_message_id = result.get('message_id')
        screen.phase = 'qr'
        screen.notice = 'Ожидаем подтверждение в MAX…'
        await self.render(screen)

    async def clear_qr(self, screen):
        message_id = screen.qr_message_id
        screen.qr_message_id = None
        if message_id:
            await self.tg.remove(screen.owner, message_id)

    async def start(self, owner, disconnect=False):
        existing = self.screens.get(owner)
        if existing and existing.phase == 'disconnecting':
            raise Rejected('Отключение уже выполняется.')
        await self.cancel(owner, display=False)
        if not disconnect and await self.db.account(owner):
            raise Rejected(
                'MAX уже подключён. /status — состояние, '
                '/disconnect — сменить аккаунт.'
            )
        if len(self.screens) >= self.settings.max_accounts * 4:
            raise Rejected('Слишком много одновременных входов. Повторите позже.')
        screen = Screen(
            owner=owner,
            phase='disconnect' if disconnect else 'consent',
            expires=time.monotonic() + self.settings.auth_ttl,
        )
        self.screens[owner] = screen
        await self.render(screen)

    async def cancel(self, owner, display=True):
        screen = self.screens.get(owner)
        if not screen:
            return
        if screen.phase == 'disconnecting' and display:
            raise Rejected('Отключение уже выполняется.')
        screen.wipe()
        if screen.task and not screen.task.done():
            screen.task.cancel()
            await asyncio.gather(screen.task, return_exceptions=True)
        await self.clear_qr(screen)
        if screen.account_id:
            await self.db.pool.execute(
                "DELETE FROM accounts WHERE id=$1 AND owner=$2 "
                "AND status='authorizing'",
                screen.account_id,
                owner,
            )
        screen.phase, screen.notice = 'cancelled', 'Вход отменён.'
        if display:
            with contextlib.suppress(BridgeError):
                await self.render(screen)
        self.screens.pop(owner, None)
        old = self.renders.pop(owner, None)
        if old and not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        self.render_locks.pop(owner, None)

    async def callback(self, query):
        owner = query.get('from', {}).get('id')
        screen = self.screens.get(owner)
        if not screen:
            return
        async with screen.lock:
            key = valid_callback(screen, query)
            if key is None:
                return
            if key == 'cancel':
                cancel = True
            else:
                cancel = False
                if screen.phase == 'consent' and key == 'accept':
                    await self.db.consent(owner)
                    screen.phase = 'phone'
                elif screen.phase == 'disconnect' and key == 'confirm':
                    screen.phase = 'disconnecting'
                    screen.task = asyncio.create_task(self._disconnect(screen))
                elif screen.phase == 'phone' and key in ('submit', 'qr'):
                    screen.phone = validate_phone(screen.buffer)
                    screen.buffer, screen.phase = '', 'requesting'
                    screen.auth_stage = 'connecting'
                    screen.notice = STAGES['connecting']
                    screen.trace_id = secrets.token_hex(4)
                    kind = 'web' if key == 'qr' else 'mobile'
                    screen.task = asyncio.create_task(self._login(screen, kind))
                elif screen.phase == 'code' and key == 'submit':
                    length_ok = (
                        len(screen.buffer) == screen.code_length
                        if screen.code_length
                        else 4 <= len(screen.buffer) <= 8
                    )
                    if (
                        not screen.buffer.isascii()
                        or not screen.buffer.isdecimal()
                        or not length_ok
                    ):
                        expected = (
                            str(screen.code_length)
                            if screen.code_length
                            else '4–8'
                        )
                        raise Rejected(f'Введите {expected} цифр кода.')
                    if screen.future and not screen.future.done():
                        value = screen.buffer
                        screen.buffer = ''
                        screen.phase = 'checking'
                        screen.future.set_result(value)
                elif screen.phase == 'code' and key == 'qr':
                    if not screen.future or screen.future.done():
                        raise Rejected('Начните вход заново: /connect.')
                    screen.buffer, screen.phase = '', 'requesting'
                    screen.auth_stage = 'requesting_qr'
                    screen.notice = 'Переключаемся на QR…'
                    screen.future.set_exception(SwitchToQr())
                else:
                    apply_key(screen, key)
        if cancel:
            await self.cancel(owner)
        else:
            self.refresh(screen)

    async def password(self, message):
        screen = self.screens.get(message['from']['id'])
        if not screen:
            return False
        if screen.phase == 'qr':
            await self.tg.remove(screen.owner, message['message_id'])
            return True
        if screen.phase != 'password':
            return False
        value = message.get('text', '')
        await self.tg.remove(screen.owner, message['message_id'])
        if value.startswith('/'):
            await self.cancel(screen.owner)
            return True
        async with screen.lock:
            if time.monotonic() > screen.expires:
                raise Rejected('Время входа истекло. /connect')
            if not value or len(value) > 256:
                raise Rejected('Некорректный пароль.')
            if screen.future and not screen.future.done():
                screen.phase = 'checking'
                screen.future.set_result(value)
        self.refresh(screen)
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

    async def _login(self, screen, kind='mobile'):
        try:
            log.info('MAX auth attempt=%s stage=account_setup', screen.trace_id)
            account = await self.db.new_account(
                screen.owner,
                screen.phone,
                self.settings.max_accounts,
            )
            screen.account_id = account['id']
            async with asyncio.timeout(
                max(0.1, screen.expires - time.monotonic())
            ):
                while True:
                    provider = Providers(self, screen, kind)
                    await provider.progress('connecting')
                    try:
                        await self._run_attempt(account, provider, kind)
                        break
                    except SwitchToQr:
                        kind = 'web'
                        await self.clear_qr(screen)
                        screen.phase = 'requesting'
                        screen.auth_stage = 'requesting_qr'
                        screen.notice = 'Создаём QR…'
                        self.refresh(screen)
                        continue
            screen.phase, screen.account_id = 'ready', None
            log.info(
                'MAX auth attempt=%s stage=ready client=%s',
                screen.trace_id,
                kind,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category, description = describe_failure(exc, screen.auth_stage)
            code = safe_error_code(exc)
            log.warning(
                'MAX auth attempt=%s stage=%s category=%s code=%s',
                screen.trace_id,
                screen.auth_stage,
                category,
                code,
            )
            screen.phase = 'error'
            screen.notice = (
                str(exc) if isinstance(exc, BridgeError) else description
            )
            screen.notice += (
                f'\nДиагностика: {category} / {code}. '
                f'Попытка: {screen.trace_id}. /connect — начать заново.'
            )
        finally:
            await self.clear_qr(screen)
            screen.wipe()
            try:
                if screen.account_id:
                    await self.db.pool.execute(
                        "DELETE FROM accounts WHERE id=$1 AND owner=$2 "
                        "AND status='authorizing'",
                        screen.account_id,
                        screen.owner,
                    )
                    screen.account_id = None
            except Exception:
                log.warning(
                    'MAX auth attempt=%s stage=cleanup category=DATABASE',
                    screen.trace_id,
                )
            if self.screens.get(screen.owner) is screen:
                self.refresh(screen)

    async def _disconnect(self, screen):
        try:
            revoked = await self.hub.disconnect(screen.owner)
            screen.phase = 'done'
            screen.notice = 'MAX отключён.'
            if not revoked:
                screen.notice += ' Завершите сессию также в MAX → Устройства.'
        except Exception as exc:
            log.warning('Disconnect failed (%s)', type(exc).__name__)
            screen.phase = 'error'
            screen.notice = 'Не удалось отключить MAX. Повторите /disconnect.'
        finally:
            self.refresh(screen)

    async def expire(self):
        for owner, screen in list(self.screens.items()):
            if time.monotonic() > screen.expires:
                await self.cancel(
                    owner,
                    display=screen.phase not in ('ready', 'done'),
                )

    async def close(self):
        for owner in list(self.screens):
            await self.cancel(owner, display=False)
