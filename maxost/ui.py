from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from .bot_text import auth_notice
from .errors import Rejected


@dataclass
class Screen:
    owner: int
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(8))
    phase: str = 'consent'
    buffer: str = field(default='', repr=False)
    phone: str = field(default='', repr=False)
    message_id: int | None = None
    qr_message_id: int | None = None
    auth_stage: str = 'connecting'
    trace_id: str = field(default_factory=lambda: secrets.token_hex(4))
    code_length: int | None = None
    expires: float = field(default_factory=lambda: time.monotonic() + 600)
    future: asyncio.Future | None = field(default=None, repr=False)
    account_id: object = None
    seen: set[str] = field(default_factory=set, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    notice: str = ''
    task: asyncio.Task | None = field(default=None, repr=False)

    def wipe(self):
        self.buffer = self.phone = ''
        if self.future and not self.future.done():
            self.future.cancel()
        self.future = None
        self.seen.clear()


def button(text, callback, style=None):
    result = {'text': text, 'callback_data': callback}
    if style:
        result['style'] = style
    return result


def row(buttons):
    return {'type': 'buttons', 'buttons': buttons, 'align': 'center'}


def paragraph(text):
    return {'type': 'paragraph', 'text': text}


def card(screen: Screen) -> dict:
    prefix = f'a:{screen.nonce}:{screen.phase}:'
    blocks = [{'type': 'heading', 'size': 2, 'text': 'MAXOST'}]
    phase = screen.phase

    if phase == 'consent':
        blocks += [
            paragraph('Ваши чаты MAX — в топиках Telegram.'),
            paragraph(
                'Подключайте только свой аккаунт. Сервис получает доступ '
                'к переписке и отправляет сообщения от вашего имени.'
            ),
            {
                'type': 'details',
                'summary': 'О подключении',
                'is_open': False,
                'blocks': [
                    paragraph(
                        'Неофициальный сервис. Возможны ограничения MAX. '
                        'Сессии шифруются в БД, но оператор имеет технический '
                        'доступ к данным.'
                    ),
                    paragraph(
                        'Отключить аккаунт и удалить данные: /disconnect.'
                    ),
                ],
            },
            row([button('Подключить MAX', prefix + 'accept', 'success')]),
        ]
    elif phase in ('phone', 'code'):
        phone = phase == 'phone'
        display = (
            '+' + screen.buffer + '▏'
            if phone
            else ' '.join('●' for _ in screen.buffer)
            + '  _' * max(0, (screen.code_length or 6) - len(screen.buffer))
        )
        blocks += [
            paragraph('Номер телефона' if phone else f'Код для …{screen.phone[-4:]}'),
            {'type': 'pre', 'text': display},
        ]
        if phone:
            blocks.append(
                paragraph('С кодом страны, без +. Например: 7… или 375…')
            )
        elif screen.notice and 'Код не принят' in screen.notice:
            blocks.append(paragraph('Код не принят. Попробуйте ещё раз.'))
        keys = [
            ('1', '1'), ('2  ABC', '2'), ('3  DEF', '3'),
            ('4  GHI', '4'), ('5  JKL', '5'), ('6  MNO', '6'),
            ('7  PQRS', '7'), ('8  TUV', '8'), ('9  WXYZ', '9'),
            ('C', 'clear'), ('0  +', '0'), ('⌫', 'back'),
        ]
        blocks += [
            row([
                button(label, prefix + key)
                for label, key in keys[index:index + 3]
            ])
            for index in range(0, 12, 3)
        ]
        blocks += [
            row([
                button(
                    'Получить код' if phone else 'Войти в MAX',
                    prefix + 'submit',
                    'success',
                )
            ]),
            row([
                button(
                    'Войти по QR' if phone else 'Нет кода? Войти по QR',
                    prefix + 'qr',
                    'primary',
                )
            ]),
            row([button('Отмена', prefix + 'cancel')]),
        ]
    elif phase == 'qr':
        blocks += [
            paragraph('Отсканируйте QR в MAX и подтвердите вход.'),
            row([button('Отмена', prefix + 'cancel')]),
        ]
    elif phase == 'password':
        blocks += [
            paragraph('Введите пароль двухэтапной проверки MAX.'),
            paragraph(
                'Пришлите следующим сообщением. Бот попробует удалить его, '
                'но Telegram получит текст.'
            ),
            row([button('Отмена', prefix + 'cancel')]),
        ]
    elif phase == 'disconnecting':
        blocks.append(paragraph('Отключаем MAX…'))
    elif phase in ('requesting', 'checking'):
        stages = {
            'connecting': 'Подключаемся…',
            'requesting_code': 'Запрашиваем код…',
            'requesting_qr': 'Создаём QR…',
            'checking_code': 'Проверяем код…',
            'checking_password': 'Проверяем пароль…',
            'confirming_qr': 'Проверяем вход…',
            'login': 'Завершаем подключение…',
        }
        blocks += [
            paragraph(stages.get(screen.auth_stage, 'Подключаемся…')),
            row([button('Отмена', prefix + 'cancel')]),
        ]
    elif phase == 'ready':
        blocks += [
            paragraph({'type': 'bold', 'text': '✓ MAX подключён'}),
            paragraph('Отвечайте прямо в топике нужного чата.'),
            row([
                button('Статус', 'nav:status'),
                button('Помощь', 'nav:help'),
            ]),
        ]
    elif phase == 'disconnect':
        blocks += [
            paragraph('Отключить MAX?'),
            paragraph(
                'Сессия, привязки топиков и очередь будут удалены. '
                'Сообщения в чатах останутся.'
            ),
            row([
                button('Отключить', prefix + 'confirm', 'danger'),
                button('Отмена', prefix + 'cancel'),
            ]),
        ]
    else:
        blocks += [
            paragraph(auth_notice(screen) or 'Подключите MAX, чтобы начать.'),
            row([button('Подключить MAX', 'nav:connect', 'primary')]),
        ]
    return {'blocks': blocks, 'skip_entity_detection': True}


def valid_callback(screen, callback) -> str | None:
    message = callback.get('message', {})
    if (
        callback.get('from', {}).get('id') != screen.owner
        or message.get('chat', {}).get('type') != 'private'
        or message.get('chat', {}).get('id') != screen.owner
        or message.get('message_id') != screen.message_id
        or time.monotonic() > screen.expires
    ):
        return None
    prefix = f'a:{screen.nonce}:{screen.phase}:'
    data = callback.get('data', '')
    if not data.startswith(prefix) or callback['id'] in screen.seen:
        return None
    screen.seen.add(callback['id'])
    if len(screen.seen) > 2000:
        raise Rejected('Слишком много нажатий. Начните вход заново: /connect.')
    return data[len(prefix):]


def apply_key(screen, key):
    if screen.phase not in ('phone', 'code'):
        return
    if key in '0123456789' and len(key) == 1:
        limit = 15 if screen.phase == 'phone' else (screen.code_length or 8)
        if len(screen.buffer) < limit:
            screen.buffer += key
    elif key == 'back':
        screen.buffer = screen.buffer[:-1]
    elif key == 'clear':
        screen.buffer = ''


def validate_phone(digits):
    if (
        not digits.isascii()
        or not digits.isdecimal()
        or not 8 <= len(digits) <= 15
        or digits[0] == '0'
    ):
        raise Rejected(
            'Введите 8–15 цифр международного номера. '
            'Первая цифра не должна быть нулём.'
        )
    return '+' + digits
