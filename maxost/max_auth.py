"""Explicit user-initiated SMS flow for the pinned PyMax revision.

One AUTH_REQUEST per login attempt; code retries reuse its in-memory token.
This adapter never bypasses server challenges or registers a new MAX account.
"""
from __future__ import annotations

import asyncio

from .auth_diagnostics import INVALID_CODES, safe_error_code
from .errors import Rejected


class InteractiveLogin:
    def __init__(self, provider, timeout=30):
        self.provider, self.timeout = provider, timeout

    async def request(self, stage, method, *args):
        await self.provider.progress(stage)
        async with asyncio.timeout(self.timeout):
            return await method(*args)

    async def authenticate(self, app):
        from pymax.auth.models import AuthResult
        from pymax.auth.exceptions import PasswordAttemptsExceededError
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
                await self.provider.progress('waiting_code', 'Код не принят. Введите его ещё раз; новый код не запрашивался.')
            finally:
                code = ''

        token = result.login_token
        if not token and result.password_challenge:
            challenge = result.password_challenge
            for _ in range(3):
                password = await self.provider.get_password()
                try:
                    response = await self.request('checking_password', app.api.auth.check_password, challenge.track_id, password)
                except ApiError:
                    # A server rejection is not evidence of an incorrect password.
                    # In particular, do not turn rate limits into retry loops.
                    raise
                finally:
                    password = ''
                if response.login_token:
                    token = response.login_token
                    break
                if response.error:
                    await self.provider.progress('waiting_password', 'MAX не принял дополнительный пароль. Повторите ввод.')
                else:
                    raise Rejected('MAX не вернул результат проверки пароля. Начните вход заново: /connect.')
            if not token:
                raise PasswordAttemptsExceededError()
        if not token:
            if result.register_token:
                raise Rejected('На этом номере нет готового аккаунта MAX. Сначала зарегистрируйтесь в официальном приложении.')
            raise Rejected('MAX не вернул сессию. Повторная отправка кода автоматически не выполняется.')
        await self.provider.progress('login')
        return AuthResult(token=token)
