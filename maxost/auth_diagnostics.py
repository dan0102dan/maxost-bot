"""Safe login diagnostics: never log exception messages or MAX payloads."""
from __future__ import annotations

import re
import socket
import ssl

STAGES = {
    'connecting': 'Подключаемся к MAX…',
    'requesting_code': 'Запрашиваем код подтверждения у MAX…',
    'waiting_code': 'MAX принял запрос кода. Ожидаем ваш ввод.',
    'checking_code': 'Проверяем код в MAX…',
    'requesting_qr': 'Запрашиваем QR-код у MAX…',
    'waiting_qr': 'QR создан. Подтвердите вход в приложении MAX.',
    'confirming_qr': 'MAX увидел подтверждение QR. Открываем сессию…',
    'waiting_password': 'MAX запросил дополнительный пароль.',
    'checking_password': 'Проверяем дополнительный пароль…',
    'login': 'Подтверждение принято. Открываем сессию MAX…',
}
_CODE = re.compile(
    r'(?:FAIL_[A-Z_]{2,48}|(?:auth|sms|phone|code|password|limit|flood|session|client|version|captcha)\.[a-z_.]{2,48})\Z'
)
INVALID_CODES = frozenset(
    {'FAIL_VERIFY_CODE', 'FAIL_CODE_INVALID', 'auth.code.invalid', 'sms.code.invalid'}
)


def safe_error_code(exc: Exception) -> str:
    value = getattr(exc, 'error', None)
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return 'API_REJECTED' if type(exc).__name__ == 'ApiError' else 'UNCLASSIFIED'


def _chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def describe_failure(exc: Exception, stage: str) -> tuple[str, str]:
    """Return a finite diagnostic category and safe user-facing explanation."""
    errors = list(_chain(exc))
    code = safe_error_code(exc)
    if any(isinstance(e, socket.gaierror) for e in errors):
        return 'DNS', 'Сервер бота не смог определить адрес MAX. Проверьте DNS внутри Docker.'
    if any(isinstance(e, ssl.SSLError) for e in errors):
        return (
            'TLS',
            'Не удалось установить защищённое соединение с MAX. '
            'Проверьте сеть и сертификаты сервера.',
        )
    if isinstance(exc, TimeoutError):
        if stage in ('waiting_code', 'waiting_password'):
            return (
                'INPUT_TIMEOUT',
                'Время ввода подтверждения истекло. Начните новую попытку: /connect.',
            )
        if stage == 'waiting_qr':
            return 'QR_TIMEOUT', 'QR-код MAX истёк. Начните новую попытку: /connect.'
        return (
            'NETWORK_TIMEOUT',
            'MAX не ответил вовремя. Автоматический повтор авторизации не выполняется.',
        )
    if any(isinstance(e, (ConnectionError, OSError)) for e in errors):
        return (
            'CONNECTION',
            'Соединение с MAX прервалось. Проверьте доступность MAX с сервера бота.',
        )
    if type(exc).__name__ in ('ValidationError', 'VersionNotFoundError') or isinstance(
        exc, (ImportError, ValueError)
    ):
        return (
            'CLIENT_COMPATIBILITY',
            'Ошибка совместимости клиента MAX. Нужна проверка версии PyMax и протокола.',
        )
    if type(exc).__name__ == 'PasswordAttemptsExceededError':
        return (
            'PASSWORD_ATTEMPTS',
            'Дополнительный пароль не принят. Лимит попыток исчерпан.',
        )
    if type(exc).__name__ == 'ApiError':
        lower = code.lower()
        if any(word in lower for word in ('limit', 'flood', 'rate')):
            return (
                'RATE_LIMIT',
                'MAX ограничил попытки входа. Не запускайте авторизацию подряд; повторите позже.',
            )
        if code in INVALID_CODES:
            return 'INVALID_CODE', 'MAX не принял код подтверждения.'
        if 'phone' in lower:
            return (
                'PHONE_REJECTED',
                'MAX отклонил номер. Проверьте международный формат и код страны.',
            )
        if stage == 'requesting_code':
            return 'REQUEST_REJECTED', 'MAX отклонил запрос кода подтверждения.'
        if stage in ('requesting_qr', 'confirming_qr'):
            return 'QR_REJECTED', 'MAX отклонил QR-авторизацию. Создайте новый QR через /connect.'
        return 'API_REJECTED', 'MAX отклонил подтверждение входа.'
    return (
        'INTERNAL',
        'Вход остановлен из-за внутренней ошибки. Передайте оператору '
        'идентификатор попытки, но не номер, код или пароль.',
    )
