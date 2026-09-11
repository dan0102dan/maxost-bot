from __future__ import annotations

import asyncio
import html
import secrets
import time
from dataclasses import dataclass, field

from .errors import Rejected


@dataclass
class Screen:
    owner: int
    nonce: str = field(default_factory=lambda:secrets.token_urlsafe(8))
    phase: str = 'consent'
    buffer: str = field(default='',repr=False)
    phone: str = field(default='',repr=False)
    message_id: int | None = None
    expires: float = field(default_factory=lambda:time.monotonic()+600)
    future: asyncio.Future | None = field(default=None,repr=False)
    account_id: object = None
    seen: set[str] = field(default_factory=set,repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock,repr=False)
    notice: str = ''
    task: asyncio.Task | None = field(default=None,repr=False)

    def wipe(self):
        self.buffer = self.phone = ''
        if self.future and not self.future.done():
            self.future.cancel()
        self.future = None
        self.seen.clear()


def button(text, callback, style=None):
    b = {'text':text,'callback_data':callback}
    if style:
        b['style'] = style
    return b


def row(buttons):
    return {'type':'buttons','buttons':buttons,'align':'center'}


def paragraph(text):
    return {'type':'paragraph','text':text}


def card(screen: Screen) -> dict:
    prefix = f'a:{screen.nonce}:{screen.phase}:'
    blocks = [
        {'type':'heading','size':2,'text':'MAXOST'},
        {'type':'footer','text':'MAX ↔ Telegram · Ваши диалоги в одном месте'},
        {'type':'divider'},
    ]
    phase = screen.phase
    if phase == 'consent':
        blocks += [
            paragraph({'type':'bold','text':'Подключите личный аккаунт MAX'}),
            paragraph('Каждый собеседник появится отдельной темой. Ответ в теме будет отправлен от вашего имени в MAX.'),
            {'type':'details','summary':'Что важно знать перед входом','is_open':True,'blocks':[
                paragraph('Это независимый сервис, не официальный продукт MAX или Telegram. Неофициальный клиент может перестать работать; возможны ограничения аккаунта.'),
                paragraph('Сервис получает доступ к вашей переписке и отправке сообщений. Сессия и очередь шифруются в базе, но оператор сервера имеет технический доступ к данным. Чат с ботом не является сквозным шифрованием.'),
                paragraph('Подключайте только свой аккаунт. SMS-коды не сохраняются. Отключить сессию и удалить данные можно командой /disconnect.'),
            ]},
            row([button('Подключить свой MAX',prefix+'accept','success')]),
        ]
    elif phase in ('phone','code'):
        phone = phase == 'phone'
        title = '01 / Номер телефона' if phone else '02 / Подтверждение входа'
        display = '+'+screen.buffer+'▏' if phone else ' '.join('●' for _ in screen.buffer) + '  _' * max(0,6-len(screen.buffer))
        hint = 'Введите номер с кодом страны, без +. Например: 7… или 375…' if phone else f'Введите SMS-код для номера …{screen.phone[-4:]}. Код не появится в истории чата.'
        blocks += [paragraph({'type':'bold','text':title}),{'type':'pre','text':display},paragraph(hint)]
        if screen.notice:
            blocks.append(paragraph(screen.notice))
        keys = [('1','1'),('2  ABC','2'),('3  DEF','3'),('4  GHI','4'),('5  JKL','5'),('6  MNO','6'),('7  PQRS','7'),('8  TUV','8'),('9  WXYZ','9'),('C','clear'),('0  +','0'),('⌫','back')]
        blocks += [row([button(label,prefix+key) for label,key in keys[n:n+3]]) for n in range(0,12,3)]
        blocks.append(row([button('Получить код' if phone else 'Войти в MAX',prefix+'submit','success')]))
        blocks.append(row([button('Отмена',prefix+'cancel','danger')]))
    elif phase == 'password':
        blocks += [paragraph({'type':'bold','text':'03 / Дополнительный пароль MAX'}),
            paragraph('На аккаунте включена двухэтапная проверка. Отправьте пароль следующим сообщением в этот же чат. Бот попытается сразу удалить его, но Telegram уже получит текст. Пароль не сохраняется в базе и не пересылается собеседнику.'),
            paragraph('Для отказа нажмите «Отмена».'),
            row([button('Отмена',prefix+'cancel','danger')])]
    elif phase=='disconnecting':
        blocks += [paragraph('Отключаем сессию и удаляем данные сервиса…')]
    elif phase in ('requesting','checking'):
        blocks += [paragraph('Подключаемся к MAX…' if phase=='requesting' else 'Проверяем подтверждение…'),
            row([button('Отмена',prefix+'cancel','danger')])]
    elif phase == 'ready':
        blocks += [paragraph({'type':'bold','text':'✓ Аккаунт подключён'}),
            paragraph('Новые личные сообщения MAX появятся в отдельных темах. Отвечайте прямо в теме нужного собеседника.'),
            paragraph('Старая переписка до подключения не импортируется.'),
            row([button('Состояние','nav:status','primary'),button('Помощь','nav:help')])]
    elif phase == 'disconnect':
        blocks += [paragraph('Отключить аккаунт и удалить данные сервиса?'),
            paragraph('Будут удалены сессия, привязки тем и очередь. Сообщения в Telegram и MAX останутся. Непереданные сообщения перестанут доставляться.'),
            row([button('Отключить и удалить',prefix+'confirm','danger'),button('Отмена',prefix+'cancel')])]
    else:
        blocks += [paragraph(screen.notice or 'Вход завершён. Используйте /connect для новой попытки.'),
            row([button('Подключить MAX','nav:connect','primary')])]
    return {'blocks':blocks,'skip_entity_detection':True}


def fallback(rich):
    """Explicit compatibility mode for Bot API/client deployments without RichMessage."""
    lines, keyboard = [], []
    def walk(blocks):
        for b in blocks:
            if b['type']=='buttons':
                keyboard.append(b['buttons'])
            elif b['type']=='details':
                lines.append(html.escape(b['summary']))
                walk(b['blocks'])
            elif b['type']=='divider':
                lines.append('──────────────')
            else:
                text = b.get('text','')
                if isinstance(text,dict):
                    text = text.get('text','')
                text = html.escape(text)
                if b['type']=='heading':
                    text = '<b>'+text+'</b>'
                if b['type']=='pre':
                    text = '<pre>'+text+'</pre>'
                lines.append(text)
    walk(rich['blocks'])
    return '\n\n'.join(lines), {'inline_keyboard':keyboard}


def valid_callback(screen, callback) -> str | None:
    msg = callback.get('message',{})
    if (callback.get('from',{}).get('id') != screen.owner or msg.get('chat',{}).get('type')!='private'
            or msg.get('chat',{}).get('id') != screen.owner or msg.get('message_id') != screen.message_id
            or time.monotonic()>screen.expires):
        return None
    prefix = f'a:{screen.nonce}:{screen.phase}:'
    data = callback.get('data','')
    if not data.startswith(prefix) or callback['id'] in screen.seen:
        return None
    screen.seen.add(callback['id'])
    if len(screen.seen) > 2000:
        raise Rejected('Слишком много нажатий. Начните вход заново: /connect.')
    return data[len(prefix):]


def apply_key(screen, key):
    """Pure keypad reducer; never submits authentication by itself."""
    if screen.phase not in ('phone','code'):
        return
    if key in '0123456789' and len(key)==1:
        if len(screen.buffer) < (15 if screen.phase=='phone' else 8):
            screen.buffer += key
    elif key=='back':
        screen.buffer = screen.buffer[:-1]
    elif key=='clear':
        screen.buffer = ''


def validate_phone(digits):
    if not digits.isascii() or not digits.isdecimal() or not 8 <= len(digits) <= 15 or digits[0]=='0':
        raise Rejected('Введите 8–15 цифр международного номера. Первая цифра не должна быть нулём.')
    return '+'+digits
