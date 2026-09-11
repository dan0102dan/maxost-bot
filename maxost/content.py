from __future__ import annotations

from pathlib import PurePath

from .errors import Rejected


SUPPORTED_TG = {'photo','document','video','voice','audio','video_note'}


def split_text(text, limit=3500):
    """Bound UTF-16 length (including astral emoji); never split a code point."""
    result, chars, size = [], [], 0
    for ch in text:
        width = 2 if ord(ch)>0xffff else 1
        if size+width > limit:
            result.append(''.join(chars))
            chars, size = [], 0
        chars.append(ch)
        size += width
    if chars or not result:
        result.append(''.join(chars))
    return result


def safe_name(name, default='attachment.bin'):
    clean = PurePath(str(name or default).replace('\\','/')).name
    clean = ''.join(c for c in clean if ord(c)>=32 and c not in '/\\')[:160]
    return clean if clean not in ('','.','..') else default


def from_telegram(message, limit):
    text = message.get('text',message.get('caption',''))
    attachment = None
    for kind in SUPPORTED_TG:
        if kind not in message:
            continue
        source = message[kind][-1] if kind=='photo' else message[kind]
        if source.get('file_size',0)>limit:
            raise Rejected('Вложение слишком большое. Лимит первой версии — 20 МБ.')
        defaults = {'photo':'photo.jpg','voice':'voice.ogg','video':'video.mp4','video_note':'circle.mp4','audio':'audio.mp3'}
        attachment = {'kind':kind,'file_id':source['file_id'],
            'name':safe_name(source.get('file_name'),defaults.get(kind,'document.bin')),
            'duration':int(source.get('duration',0))*1000}
        break
    if not text and attachment is None:
        raise Rejected('Этот тип сообщения пока не поддерживается. Отправьте текст, фото, файл, видео или голосовое.')
    parts = []
    if attachment:
        # Separate text means no truncated captions and simpler deterministic message mapping.
        if text:
            parts.extend({'text':s} for s in split_text(text))
        parts.append({'text':'','attachment':attachment})
    else:
        parts.extend({'text':s} for s in split_text(text))
    reply = message.get('reply_to_message',{}).get('message_id')
    for part in parts:
        part['reply_to'] = reply
    return parts


def is_private_update(message):
    return (message.get('chat',{}).get('type')=='private'
        and message.get('chat',{}).get('id') == message.get('from',{}).get('id')
        and not message.get('from',{}).get('is_bot',False))


def is_dialog(chat):
    kind = getattr(chat,'type',None)
    return str(getattr(kind,'value',kind)).upper() == 'DIALOG'
