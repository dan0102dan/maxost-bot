"""Entity conversion without reparsing user text as Markdown (UTF-16 offsets)."""
from __future__ import annotations

from urllib.parse import urlsplit

MAX_TO_TG = {
    'STRONG': 'bold', 'HEADING': 'bold', 'EMPHASIZED': 'italic',
    'UNDERLINE': 'underline', 'STRIKETHROUGH': 'strikethrough',
    'MONOSPACED': 'code', 'CODE': 'pre', 'QUOTE': 'blockquote', 'LINK': 'text_link',
    'SPOILER': 'spoiler',
}
TG_TO_MAX = {v: k for k, v in MAX_TO_TG.items() if k not in ('HEADING', 'SPOILER')}
TG_TO_MAX['expandable_blockquote'] = 'QUOTE'
AUTOMATIC = {'mention', 'hashtag', 'cashtag', 'bot_command', 'url', 'email', 'phone_number'}


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def utf16(text):
    return len(text.encode('utf-16-le')) // 2


def boundaries(text):
    offsets, size = {0}, 0
    for char in text:
        size += 2 if ord(char) > 0xffff else 1
        offsets.add(size)
    return offsets


def safe_link(url):
    try:
        return isinstance(url, str) and urlsplit(url).scheme.lower() in {'https', 'http', 'tg', 'mailto', 'tel'}
    except ValueError:
        return False


def valid_entities(text, entities):
    valid, offsets = [], boundaries(text)
    for entity in entities or []:
        start, length = entity.get('offset'), entity.get('length')
        if not isinstance(start, int) or not isinstance(length, int) or length <= 0:
            continue
        if start not in offsets or start + length not in offsets:
            continue
        if entity['type'] == 'text_link' and not safe_link(entity.get('url')):
            continue
        valid.append(dict(entity))
    return sorted(valid, key=lambda e: (e['offset'], -e['length']))


def from_max(text, elements):
    entities, unsupported = [], set()
    for element in elements or []:
        kind = str(value(element, 'type', '')).upper()
        mapped = MAX_TO_TG.get(kind)
        if not mapped:
            unsupported.add(kind or 'UNKNOWN')
            continue
        e = {'type': mapped, 'offset': value(element, 'from_') if value(element, 'from_') is not None else value(element, 'from', 0),
             'length': value(element, 'length', 0)}
        if mapped == 'text_link':
            e['url'] = value(value(element, 'attributes', {}), 'url')
        entities.append(e)
    return valid_entities(text, entities), sorted(unsupported)


def to_max(text, entities):
    """Return MAX wire elements and explicit notes for non-equivalent features."""
    result, unsupported = [], set()
    for e in valid_entities(text, entities):
        kind = e['type']
        if kind in AUTOMATIC:
            continue  # Text (including literal URL/mention) is unchanged.
        if kind == 'text_mention':
            uid = e.get('user', {}).get('id')
            if not isinstance(uid, int) or uid <= 0:
                unsupported.add(kind)
                continue
            result.append({'type': 'LINK', 'from': e['offset'], 'length': e['length'],
                           'attributes': {'url': f'tg://user?id={uid}'}})
            continue
        mapped = TG_TO_MAX.get(kind)
        if not mapped:
            unsupported.add(kind)
            continue
        item = {'type': mapped, 'from': e['offset'], 'length': e['length']}
        if kind == 'text_link':
            item['attributes'] = {'url': e['url']}
        result.append(item)
        if kind == 'pre' and e.get('language'):
            unsupported.add('code_language')
        if kind == 'expandable_blockquote':
            unsupported.add('expandable_blockquote')
    return result, sorted(unsupported)


def chunks(text, entities=(), limit=3500):
    """Split text and clip/rebase entities without splitting surrogate pairs."""
    if limit < 2:
        raise ValueError('limit must be at least two UTF-16 code units')
    entities = valid_entities(text, entities)
    pieces, chars, size, offset = [], [], 0, 0
    for char in text:
        width = 2 if ord(char) > 0xffff else 1
        if size + width > limit:
            pieces.append((''.join(chars), offset, size))
            offset, chars, size = offset + size, [], 0
        chars.append(char)
        size += width
    if chars or not pieces:
        pieces.append((''.join(chars), offset, size))
    result = []
    for text_part, start, length in pieces:
        local = []
        for e in entities:
            left, right = max(start, e['offset']), min(start + length, e['offset'] + e['length'])
            if right > left:
                local.append({**e, 'offset': left - start, 'length': right - left})
        result.append({'text': text_part, 'entities': local})
    return result


def prepend(payload, prefix):
    payload = dict(payload)
    payload['text'] = prefix + payload.get('text', '')
    payload['entities'] = ([{'type': 'bold', 'offset': 0, 'length': utf16(prefix.rstrip())}]
        + [{**e, 'offset': e['offset'] + utf16(prefix)} for e in payload.get('entities', [])])
    return payload
