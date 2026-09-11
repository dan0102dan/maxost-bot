from __future__ import annotations

from pathlib import PurePath

from .errors import Rejected
from .formatting import chunks, valid_entities, value

SUPPORTED_TG = ('photo', 'animation', 'sticker', 'video', 'voice', 'audio', 'video_note', 'document')


def split_text(text, limit=3500):
    return [p['text'] for p in chunks(text, limit=limit)]


def safe_name(name, default='attachment.bin'):
    clean = PurePath(str(name or default).replace('\\', '/')).name
    clean = ''.join(c for c in clean if ord(c) >= 32 and c not in '/\\')[:160]
    return clean if clean not in ('', '.', '..') else default


def canonical(payload):
    """Accept queued v0.1 single-part payloads during an in-place upgrade."""
    p = dict(payload)
    p.setdefault('text', '')
    p.setdefault('entities', [])
    p.setdefault('attachments', [p['attachment']] if p.get('attachment') else [])
    return p


def from_telegram(message, limit):
    text = message.get('text', message.get('caption', '')) or ''
    entities = valid_entities(text, message.get('entities', message.get('caption_entities', [])))
    attachments = []
    for kind in SUPPORTED_TG:
        if kind not in message:
            continue
        source = message[kind][-1] if kind == 'photo' else message[kind]
        if source.get('file_size', 0) > limit:
            raise Rejected('Вложение превышает настроенный лимит размера.')
        defaults = {'photo': 'photo.jpg', 'voice': 'voice.ogg', 'video': 'video.mp4',
                    'video_note': 'circle.mp4', 'audio': 'audio.mp3', 'animation': 'animation.mp4',
                    'sticker': 'sticker.tgs' if source.get('is_animated') else
                        'sticker.webm' if source.get('is_video') else 'sticker.webp'}
        attachments.append({'kind': kind, 'file_id': source['file_id'],
            'file_unique_id': source.get('file_unique_id'),
            'name': safe_name(source.get('file_name'), defaults.get(kind, 'document.bin')),
            'duration': int(source.get('duration', 0)) * 1000,
            'is_animated': source.get('is_animated', False), 'is_video': source.get('is_video', False),
            'emoji': source.get('emoji', ''), 'tg_id': message.get('message_id')})
        break
    if 'poll' in message:
        poll = message['poll']
        if poll.get('type') == 'quiz':
            raise Rejected('Telegram не передаёт боту правильный ответ чужой викторины. Создайте обычный опрос; викторина не будет молча превращена в него.')
        if any(o.get('media') for o in poll.get('options', [])) or poll.get('media'):
            raise Rejected('Опрос с медиа-вариантами не имеет эквивалента в MAX. Отправьте текстовый опрос.')
        attachments.append({'kind': 'poll', 'poll': {
            'title': poll['question'], 'answers': [{'text': o['text']} for o in poll['options']],
            'settings': (1 if poll.get('is_anonymous', True) else 0)
                | (2 if poll.get('allows_multiple_answers') else 0) | 4
                | (8 if poll.get('is_closed') else 0)}, 'tg_id': message.get('message_id')})
    if not text and not attachments:
        raise Rejected('Этот тип Telegram-сообщения не поддерживается; содержимое не отправлено в MAX.')
    p = {'text': text, 'entities': entities, 'attachments': attachments,
         'reply_to': message.get('reply_to_message', {}).get('message_id'),
         'tg_ids': [message['message_id']] if 'message_id' in message else [],
         'media_group_id': message.get('media_group_id')}
    return [p]


def is_private_update(message):
    return (message.get('chat', {}).get('type') == 'private'
        and message.get('chat', {}).get('id') == message.get('from', {}).get('id')
        and not message.get('from', {}).get('is_bot', False))


def chat_kind(chat):
    kind = value(chat, 'type', '')
    return str(getattr(kind, 'value', kind)).upper()


def is_dialog(chat):
    return chat_kind(chat) == 'DIALOG'


def supported_chat(chat):
    return chat_kind(chat) in ('DIALOG', 'CHAT', 'CHANNEL')


def can_publish(chat, max_user_id):
    """Conservative channel guard; MAX remains the authority for granular ACLs."""
    if chat_kind(chat) != 'CHANNEL':
        return True
    return (value(chat, 'owner') == max_user_id
        or max_user_id in (value(chat, 'admins', []) or [])
        or max_user_id in (value(chat, 'admin_participants', {}) or {}))


def combine_album(members):
    text, entities, attachments, ids = '', [], [], []
    from .formatting import utf16
    for member in sorted(members, key=lambda p: int(p['tg_ids'][0])):
        if member.get('text'):
            prefix = '\n\n' if text else ''
            shift = utf16(text + prefix)
            text += prefix + member['text']
            entities.extend({**e, 'offset': e['offset'] + shift} for e in member.get('entities', []))
        attachments.extend(member.get('attachments', []))
        ids.extend(member['tg_ids'])
    if len(attachments) > 10:
        raise Rejected('Альбом содержит больше 10 вложений; разделите его на несколько альбомов.')
    return {'text': text, 'entities': entities, 'attachments': attachments, 'tg_ids': ids,
            'reply_to': members[0].get('reply_to') if members else None}
