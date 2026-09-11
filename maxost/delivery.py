"""Content delivery and reconciliation. Checkpoint every confirmed remote operation."""
from __future__ import annotations

import hashlib
import json

from .content import canonical
from .errors import Rejected
from .formatting import chunks, utf16
from .max_client import mutate
from . import max_transport
from .telegram import TelegramRejected

MEDIA = {'photo', 'video', 'audio', 'document', 'animation'}
SEND = {'photo': 'sendPhoto', 'video': 'sendVideo', 'audio': 'sendAudio',
        'document': 'sendDocument', 'voice': 'sendVoice', 'sticker': 'sendSticker',
        'animation': 'sendAnimation', 'video_note': 'sendVideoNote'}


def category(kind):
    return 'visual' if kind in ('photo', 'video') else kind if kind in ('audio', 'document') else None


def tag(attachment):
    identity = attachment.get('file_id') or attachment.get('identity')
    return hashlib.sha256(json.dumps([attachment['kind'], identity]).encode()).hexdigest() if identity else None


def tg_units(payload):
    p = canonical(payload)
    attachments = p['attachments']
    units = []
    caption = bool(attachments and attachments[0]['kind'].lower() not in ('poll', 'sticker') and utf16(p['text']) <= 1024)
    if p['text'] and not caption:
        units.extend({'kind': 'text', **part} for part in chunks(p['text'], p['entities']))
    for n, attachment in enumerate(attachments):
        kind = attachment['kind'].lower()
        kind = {'file': 'document', 'audio': 'voice'}.get(kind, kind)
        units.append({'kind': kind, 'attachment': attachment,
            'text': p['text'] if caption and n == 0 else '',
            'entities': p['entities'] if caption and n == 0 else []})
    return units


def operations(units):
    """Keep compatible adjacent attachments in real Telegram media groups."""
    result = []
    i = 0
    while i < len(units):
        j = i + 1
        family = category(units[i]['kind'])
        if family:
            while j < len(units) and j - i < 10 and category(units[j]['kind']) == family:
                j += 1
        result.append((i, units[i:j]))
        i = j
    return result


def old_snapshot(rows):
    keys = ('id', 'tg_message_id', 'max_message_id', 'part', 'kind', 'origin', 'album_id', 'media_tag')
    return [{key: r.get(key) for key in keys} for r in rows]


class Delivery:
    def __init__(self, bridge):
        self.bridge = bridge
        self.db, self.tg, self.media = bridge.db, bridge.tg, bridge.media
        self.interactions = None

    async def step(self, job, name, operation):
        saved = await self.db.saved_step(job, name)
        if saved is not None:
            return saved
        await self.db.mark_sending(job)
        result = await operation()
        await self.db.save_step(job, name, result)
        return result

    async def reply(self, d, job, p):
        if not p.get('reply_to'):
            return None
        source = 'max' if job['direction'] == 'tg' else 'tg'
        links = await self.db.links(d, source, p['reply_to'])
        return links[0]['tg_message_id' if source == 'max' else 'max_message_id'] if links else None

    async def upload_tg(self, entry, d, job, unit):
        a = unit['attachment']
        return await self.media.for_telegram(entry.client, d['max_chat_id'], job['source_id'], a['index'])

    async def send_tg(self, entry, d, job, units, thread, reply):
        params = {'chat_id': d['owner'], 'message_thread_id': thread}
        if reply:
            params['reply_parameters'] = {'message_id': int(reply), 'allow_sending_without_reply': True}
        if len(units) == 1 and units[0]['kind'] == 'poll':
            return [await self.interactions.publish_poll(d, job['source_id'], units[0]['attachment']['poll'], thread, reply)]
        await self.tg.pace(d['owner'])
        if len(units) == 1 and units[0]['kind'] == 'text':
            u = units[0]
            r = await self.tg.call('sendMessage', {**params, 'text': u['text'], 'entities': u['entities'], 'link_preview_options': {'is_disabled': True}})
            return [{'id': r['message_id'], 'kind': 'text'}]
        uploads = [await self.upload_tg(entry, d, job, u) for u in units]
        if len(units) > 1:
            families = {category(u[0]) for u in uploads}
            # A large photo may need the document endpoint. Keep the album together.
            document_album = len(families) != 1 or None in families
            files, media = {}, []
            for n, (u, (kind, name, raw)) in enumerate(zip(units, uploads)):
                kind = 'document' if document_album else kind
                files[f'file{n}'] = (name, raw)
                media.append({'type': kind, 'media': f'attach://file{n}', 'caption': u['text'], 'caption_entities': u['entities']})
            result = await self.tg.call('sendMediaGroup', {**params, 'media': media}, files=files)
            return [{'id': r['message_id'], 'kind': media[n]['type'], 'album': r.get('media_group_id'), 'media_tag': tag(units[n]['attachment'])} for n, r in enumerate(result)]
        u, (kind, name, raw) = units[0], uploads[0]
        if kind not in ('sticker', 'video_note'):
            params.update(caption=u['text'], caption_entities=u['entities'])
        try:
            r = await self.tg.call(SEND[kind], params, files={kind: (name, raw)})
        except TelegramRejected as exc:
            if kind != 'sticker' or exc.code != 400:
                raise
            # Not every MAX Lottie/WebP fits Telegram sticker requirements.
            kind = 'document'
            r = await self.tg.call('sendDocument', {**params, 'caption': '[Стикер MAX: исходный файл]'}, files={'document': (name, raw)})
        return [{'id': r['message_id'], 'kind': kind, 'media_tag': tag(u['attachment'])}]

    async def remember_tg(self, job, start, result):
        await self.db.save_links(job, [{'tg': r['id'], 'max': job['source_id'], 'kind': r['kind'],
            'part': start + n, 'album': r.get('album'), 'media_tag': r.get('media_tag')} for n, r in enumerate(result)])

    async def send(self, entry, d, job, payload):
        p = canonical(payload)
        reply = await self.reply(d, job, p)
        if job['direction'] == 'tg':
            thread = await self.bridge.topic(d)
            for start, group in operations(tg_units(p)):
                result = await self.step(job, f'send:{start}', lambda group=group: self.send_tg(entry, d, job, group, thread, reply))
                await self.remember_tg(job, start, result)
        else:
            await self.send_max(entry, d, job, p, reply)

    async def send_max(self, entry, d, job, p, reply, old=None):
        prepared, notes = await max_transport.prepare(entry.client, self.media, p)
        if notes:
            p = {**p, 'text': p['text'] + '\n[' + ' '.join(sorted(set(notes))) + ']'}
        parts = chunks(p['text'], p['entities'])
        tg_ids = p.get('tg_ids') or [int(job['source_id'])]
        old = old or []
        prior = {}
        for link in old:
            prior.setdefault(link['part'], link)
        for n, part in enumerate(parts):
            link = prior.get(n)
            if link and link['origin'] != 'tg':
                raise Rejected('Нельзя редактировать чужое сообщение MAX.')
            mid = link['max_message_id'] if link else None
            result = await self.step(job, f'max:{n}', lambda part=part, n=n, mid=mid: max_transport.transmit(entry.client, d['max_chat_id'], part, prepared if n == 0 else [], reply, mid))
            kind = 'media' if n == 0 and prepared else 'text'
            await self.db.save_links(job, [{'tg': tid, 'max': result['id'], 'kind': kind, 'part': n} for tid in tg_ids])
            for poll in result.get('polls', []):
                async def show(poll=poll, mid=result['id']):
                    return await self.interactions.publish_poll(d, mid, poll, d['tg_thread_id'], tg_ids[0])
                await self.step(job, f'max-poll:{n}:{poll["poll_id"]}', show)
        for link in old:
            if link['part'] >= len(parts):
                await self.delete_link(entry, d, job, link)

    async def delete_link(self, entry, d, job, link):
        async def remove():
            if job['direction'] == 'tg':
                try:
                    await self.tg.call('deleteMessage', {'chat_id': d['owner'], 'message_id': link['tg_message_id']})
                except TelegramRejected as exc:
                    if 'message to delete not found' not in exc.description.lower():
                        raise
            else:
                if link['origin'] != 'tg':
                    raise Rejected('Нельзя удалить чужое сообщение MAX.')
                await mutate(entry.client, 'delete_message', chat_id=d['max_chat_id'], message_ids=[int(link['max_message_id'])], for_me=False)
            return {'deleted': True}
        key = link['tg_message_id'] if job['direction'] == 'tg' else link['max_message_id']
        await self.step(job, f'delete:{key}', remove)
        await self.db.remove_link(d, link['id'])

    async def change(self, entry, d, job, payload):
        if job['action'] in ('reaction', 'react', 'vote', 'poll_refresh'):
            return await self.interactions.deliver(entry, d, job, payload)
        origin = 'max' if job['direction'] == 'tg' else 'tg'
        old = await self.db.saved_step(job, 'edit-plan')
        if old is None:
            old = old_snapshot(await self.db.source_links(d, origin, job['source_id']))
            await self.db.save_step(job, 'edit-plan', old)
        if not old:
            return  # Never import history because an old message was edited/deleted.
        if job['action'] == 'delete':
            for link in old:
                await self.delete_link(entry, d, job, link)
            return
        parts = payload['parts']
        if len(parts) == 1:
            p = canonical(parts[0])
        else:  # queued v0.1 edit payload
            p = {'text': ''.join(x.get('text', '') for x in parts), 'entities': [],
                 'attachments': [x['attachment'] for x in parts if x.get('attachment')]}
        if origin == 'tg':
            return await self.send_max(entry, d, job, p, await self.reply(d, job, p), old)
        units = tg_units(p)
        thread = await self.bridge.topic(d)
        # Telegram has no "append to existing album" API. Rebuild only a group
        # whose cardinality/type family changed; same-sized media are edited in place.
        regroup = any(r.get('album_id') for r in old) and (
            len(units) != len(old) or any(category(u['kind']) != category(r['kind']) for u, r in zip(units, old)))
        if regroup:
            keep = set()
            for start, group in operations(units):
                result = await self.step(job, f'regroup:{start}', lambda group=group: self.send_tg(entry, d, job, group, thread, None))
                await self.remember_tg(job, start, result)
                keep.update(r['id'] for r in result)
            for link in old:
                if link['tg_message_id'] in keep:
                    continue
                await self.delete_link(entry, d, job, link)
            return
        for n, unit in enumerate(units):
            link = old[n] if n < len(old) else None
            compatible = link and (unit['kind'] == link['kind'] == 'text'
                or unit['kind'] in MEDIA and link['kind'] in MEDIA | {'text'}
                or unit['kind'] == link['kind'] == 'poll')
            if compatible:
                async def edit(unit=unit, link=link):
                    params = {'chat_id': d['owner'], 'message_id': link['tg_message_id']}
                    if unit['kind'] == 'poll':
                        return [await self.interactions.publish_poll(d, job['source_id'], unit['attachment']['poll'], thread, None)]
                    await self.tg.pace(d['owner'])
                    try:
                        if unit['kind'] == 'text':
                            await self.tg.call('editMessageText', {**params, 'text': unit['text'], 'entities': unit['entities'], 'link_preview_options': {'is_disabled': True}})
                            kind = 'text'
                        elif link.get('media_tag') and tag(unit['attachment']) == link['media_tag']:
                            await self.tg.call('editMessageCaption', {**params, 'caption': unit['text'], 'caption_entities': unit['entities']})
                            kind = link['kind']
                        else:
                            kind, name, raw = await self.upload_tg(entry, d, job, unit)
                            await self.tg.call('editMessageMedia', {**params, 'media': {'type': kind, 'media': 'attach://file', 'caption': unit['text'], 'caption_entities': unit['entities']}}, files={'file': (name, raw)})
                    except TelegramRejected as exc:
                        if 'message is not modified' not in exc.description.lower():
                            raise
                        kind = unit['kind']
                    return [{'id': link['tg_message_id'], 'kind': kind, 'album': link.get('album_id'), 'media_tag': tag(unit['attachment']) if unit.get('attachment') else None}]
                result = await self.step(job, f'edit:{n}', edit)
            else:
                result = await self.step(job, f'replace:{n}', lambda unit=unit: self.send_tg(entry, d, job, [unit], thread, None))
                if link:
                    await self.delete_link(entry, d, job, link)
            await self.remember_tg(job, n, result)
        for link in old[len(units):]:
            await self.delete_link(entry, d, job, link)
