"""Explicit user-initiated authentication flows for the pinned PyMax revision."""
from __future__ import annotations

import asyncio
import time

from .auth_diagnostics import INVALID_CODES, safe_error_code
from .errors import Rejected


class InteractiveLogin:
    """SMS login: exactly one AUTH_REQUEST; retries reuse its in-memory token."""

    def __init__(self, provider, timeout=30):
        self.provider, self.timeout = provider, timeout

    async def request(self, stage, method, *args):
        await self.provider.progress(stage)
        async with asyncio.timeout(self.timeout):
            return await method(*args)

    async def authenticate(self, app):
        from pymax.auth.models import AuthResult
        from pymax.exceptions import ApiError

        start = await self.request('requesting_code', app.api.auth.request_code, app.config.phone)
        if not start.token or type(start.code_length) is not int or not 1 <= start.code_length <= 12:
            raise ValueError('Unsupported code challenge')
        await self.provider.code_requested(start.code_length)
        result = None
        for attempt in range(3):
            code = await self.provider.get_code(app.config.phone)
            try:
                result = await self.request('checking_code', app.api.auth.send_code, start.token, code)
                break
            except ApiError as exc:
                if safe_error_code(exc) not in INVALID_CODES or attempt == 2:
                    raise
                await self.provider.progress(
                    'waiting_code',
                    'Код не принят. Введите его ещё раз; новый код не запрашивался.',
                )
            finally:
                code = ''

        token = result.login_token
        if not token and result.password_challenge:
            token = await self._password(app, result.password_challenge)
        if not token:
            if result.register_token:
                raise Rejected(
                    'На этом номере нет готового аккаунта MAX. '
                    'Сначала зарегистрируйтесь в официальном приложении.'
                )
            raise Rejected(
                'MAX не вернул сессию. Повторная отправка кода автоматически не выполняется.'
            )
        await self.provider.progress('login')
        return AuthResult(token=token)

    async def _password(self, app, challenge):
        from pymax.auth.exceptions import PasswordAttemptsExceededError
        from pymax.exceptions import ApiError

        for _ in range(3):
            password = await self.provider.get_password()
            try:
                response = await self.request(
                    'checking_password', app.api.auth.check_password, challenge.track_id, password
                )
            except ApiError:
                raise
            finally:
                password = ''
            if response.login_token:
                return response.login_token
            if response.error:
                await self.provider.progress(
                    'waiting_password',
                    'MAX не принял дополнительный пароль. Повторите ввод.',
                )
            else:
                raise Rejected(
                    'MAX не вернул результат проверки пароля. Начните вход заново: /connect.'
                )
        raise PasswordAttemptsExceededError()


class InteractiveQrLogin:
    """QR login for WebClient with bounded polling and the same 2FA provider."""

    def __init__(self, provider, timeout=30):
        self.provider, self.timeout = provider, timeout

    async def request(self, stage, method, *args):
        await self.provider.progress(stage)
        async with asyncio.timeout(self.timeout):
            return await method(*args)

    async def authenticate(self, app):
        from pymax.auth.models import AuthResult

        qr = await self.request('requesting_qr', app.api.auth.request_qr)
        if (
            not isinstance(qr.qr_link, str)
            or not qr.qr_link
            or len(qr.qr_link) > 4096
            or not qr.track_id
            or qr.expires_at <= int(time.time() * 1000)
        ):
            raise ValueError('Unsupported QR challenge')

        await self.provider.show_qr(qr.qr_link)
        await self.provider.progress('waiting_qr')

        interval = min(max(qr.polling_interval / 1000, 0.5), 5.0)
        expires_at = qr.expires_at / 1000
        confirmed = False
        while time.time() < expires_at:
            async with asyncio.timeout(self.timeout):
                status = await app.api.auth.check_qr(qr.track_id)
            if status.status.login_available:
                confirmed = True
                break
            await asyncio.sleep(interval)

        if not confirmed:
            raise Rejected('QR-код MAX истёк. Начните новую попытку: /connect.')

        await self.provider.qr_confirmed()
        result = await self.request('confirming_qr', app.api.auth.confirm_qr, qr.track_id)
        token = result.login_token
        if not token and result.password_challenge:
            token = await self._password(app, result.password_challenge)
        if not token:
            raise Rejected('MAX не вернул сессию после подтверждения QR. Начните вход заново.')

        await self.provider.progress('login')
        return AuthResult(token=token)

    async def _password(self, app, challenge):
        from pymax.auth.exceptions import PasswordAttemptsExceededError
        from pymax.exceptions import ApiError

        for _ in range(3):
            password = await self.provider.get_password()
            try:
                response = await self.request(
                    'checking_password', app.api.auth.check_password, challenge.track_id, password
                )
            except ApiError:
                raise
            finally:
                password = ''
            if response.login_token:
                return response.login_token
            if response.error:
                await self.provider.progress(
                    'waiting_password',
                    'MAX не принял дополнительный пароль. Повторите ввод.',
                )
            else:
                raise Rejected(
                    'MAX не вернул результат проверки пароля. Начните вход заново: /connect.'
                )
        raise PasswordAttemptsExceededError()
