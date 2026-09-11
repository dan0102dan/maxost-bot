"""Content delivery and reconciliation. Checkpoint every confirmed remote operation."""
from __future__ import annotations

import hashlib
import json

from . import max_transport
from .content import canonical
from .errors import Rejected
from .formatting import chunks, utf16
from .max_client import mutate
from .telegram import TelegramRejected

MEDIA = {'photo', 'video', 'audio', 'document', 'animation'}
SEND = {
    'photo': 'sendPhoto',
    'video': 'sendVideo',
    'audio': 'sendAudio',
    'document': 'sendDocument',
    'voice': 'sendVoice',
    'sticker': 'sendSticker',
    'animation': 'sendAnimation',
    'video_note': 'sendVideoNote',
}


def category(kind):
    return (
        'visual' if kind in ('photo', 'video')
        else kind if kind in ('audio', 'document')
        else None
    )


def tag(attachment):
    identity = attachment.get('file_id') or attachment.get('identity')
    if not identity:
        return None
    return hashlib.sha256(
        json.dumps([attachment['kind'], identity]).encode()
    ).hexdigest()


def tg_units(payload):
    normalized = canonical(payload)
    attachments = normalized['attachments']
    units = []
    caption = bool(
        attachments
        and attachments[0]['kind'].lower() not in ('poll', 'sticker')
        and utf16(normalized['text']) <= 1024
    )
    if normalized['text'] and not caption:
        units.extend(
            {'kind': 'text', **part}
            for part in chunks(normalized['text'], normalized['entities'])
        )
    for index, attachment in enumerate(attachments):
        kind = attachment['kind'].lower()
        kind = {'file': 'document', 'audio': 'voice'}.get(kind, kind)
        units.append({
            'kind': kind,
            'attachment': attachment,
            'text': normalized['text'] if caption and index == 0 else '',
            'entities': normalized['entities'] if caption and index == 0 else [],
        })
    return units


def operations(units):
    """Keep compatible adjacent attachments in real Telegram media groups."""
    result = []
    index = 0
    while index < len(units):
        end = index + 1
        family = category(units[index]['kind'])
        if family:
            while (
                end < len(units)
                and end - index < 10
                and category(units[end]['kind']) == family
            ):
                end += 1
        result.append((index, units[index:end]))
        index = end
    return result


def old_snapshot(rows):
    keys = (
        'id', 'tg_message_id', 'max_message_id', 'part',
        'kind', 'origin', 'album_id', 'media_tag',
    )
    return [{key: row.get(key) for key in keys} for row in rows]


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

    async def reply(self, dialog, job, payload):
        if not payload.get('reply_to'):
            return None
        source = 'max' if job['direction'] == 'tg' else 'tg'
        links = await self.db.links(dialog, source, payload['reply_to'])
        if not links:
            return None
        return links[0][
            'tg_message_id' if source == 'max' else 'max_message_id'
        ]

    async def upload_tg(self, entry, dialog, job, unit):
        attachment = unit['attachment']
        return await self.media.for_telegram(
            entry.client,
            dialog['max_chat_id'],
            job['source_id'],
            attachment['index'],
        )

    async def send_tg(self, entry, dialog, job, units, thread, reply):
        params = {'chat_id': dialog['owner'], 'message_thread_id': thread}
        if reply:
            params['reply_parameters'] = {
                'message_id': int(reply),
                'allow_sending_without_reply': True,
            }
        if len(units) == 1 and units[0]['kind'] == 'poll':
            return [await self.interactions.publish_poll(
                dialog,
                job['source_id'],
                units[0]['attachment']['poll'],
                thread,
                reply,
            )]

        await self.tg.pace(dialog['owner'])
        if len(units) == 1 and units[0]['kind'] == 'text':
            unit = units[0]
            result = await self.tg.call(
                'sendMessage',
                {
                    **params,
                    'text': unit['text'],
                    'entities': unit['entities'],
                    'link_preview_options': {'is_disabled': True},
                },
            )
            return [{'id': result['message_id'], 'kind': 'text'}]

        uploads = [
            await self.upload_tg(entry, dialog, job, unit) for unit in units
        ]
        if len(units) > 1:
            families = {category(upload[0]) for upload in uploads}
            document_album = len(families) != 1 or None in families
            files, media = {}, []
            for index, (unit, (kind, name, raw)) in enumerate(zip(units, uploads)):
                kind = 'document' if document_album else kind
                files[f'file{index}'] = (name, raw)
                media.append({
                    'type': kind,
                    'media': f'attach://file{index}',
                    'caption': unit['text'],
                    'caption_entities': unit['entities'],
                })
            sent = await self.tg.call(
                'sendMediaGroup', {**params, 'media': media}, files=files
            )
            return [
                {
                    'id': item['message_id'],
                    'kind': media[index]['type'],
                    'album': item.get('media_group_id'),
                    'media_tag': tag(units[index]['attachment']),
                }
                for index, item in enumerate(sent)
            ]

        unit, (kind, name, raw) = units[0], uploads[0]
        if kind not in ('sticker', 'video_note'):
            params.update(caption=unit['text'], caption_entities=unit['entities'])
        try:
            result = await self.tg.call(
                SEND[kind], params, files={kind: (name, raw)}
            )
        except TelegramRejected as exc:
            if kind != 'sticker' or exc.code != 400:
                raise
            kind = 'document'
            result = await self.tg.call(
                'sendDocument',
                {**params, 'caption': unit['text'], 'caption_entities': unit['entities']},
                files={'document': (name, raw)},
            )
        return [{
            'id': result['message_id'],
            'kind': kind,
            'media_tag': tag(unit['attachment']),
        }]

    async def remember_tg(self, job, start, result):
        await self.db.save_links(
            job,
            [
                {
                    'tg': item['id'],
                    'max': job['source_id'],
                    'kind': item['kind'],
                    'part': start + index,
                    'album': item.get('album'),
                    'media_tag': item.get('media_tag'),
                }
                for index, item in enumerate(result)
            ],
        )

    async def send(self, entry, dialog, job, payload):
        normalized = canonical(payload)
        reply = await self.reply(dialog, job, normalized)
        if job['direction'] == 'tg':
            thread = await self.bridge.topic(dialog)
            for start, group in operations(tg_units(normalized)):
                result = await self.step(
                    job,
                    f'send:{start}',
                    lambda group=group: self.send_tg(
                        entry, dialog, job, group, thread, reply
                    ),
                )
                await self.remember_tg(job, start, result)
        else:
            await self.send_max(entry, dialog, job, normalized, reply)

    async def send_max(self, entry, dialog, job, payload, reply, old=None):
        prepared = await max_transport.prepare(entry.client, self.media, payload)
        parts = chunks(payload['text'], payload['entities'])
        tg_ids = payload.get('tg_ids') or [int(job['source_id'])]
        old = old or []
        prior = {}
        for link in old:
            prior.setdefault(link['part'], link)

        for index, part in enumerate(parts):
            link = prior.get(index)
            if link and link['origin'] != 'tg':
                raise Rejected('Нельзя редактировать чужое сообщение MAX.')
            max_message_id = link['max_message_id'] if link else None
            result = await self.step(
                job,
                f'max:{index}',
                lambda part=part, index=index, max_message_id=max_message_id: (
                    max_transport.transmit(
                        entry.client,
                        dialog['max_chat_id'],
                        part,
                        prepared if index == 0 else [],
                        reply,
                        max_message_id,
                    )
                ),
            )
            kind = 'media' if index == 0 and prepared else 'text'
            await self.db.save_links(
                job,
                [
                    {
                        'tg': tg_id,
                        'max': result['id'],
                        'kind': kind,
                        'part': index,
                    }
                    for tg_id in tg_ids
                ],
            )

        for link in old:
            if link['part'] >= len(parts):
                await self.delete_link(entry, dialog, job, link)

    async def delete_link(self, entry, dialog, job, link):
        async def remove():
            if job['direction'] == 'tg':
                try:
                    await self.tg.call(
                        'deleteMessage',
                        {
                            'chat_id': dialog['owner'],
                            'message_id': link['tg_message_id'],
                        },
                    )
                except TelegramRejected as exc:
                    if 'message to delete not found' not in exc.description.lower():
                        raise
            else:
                if link['origin'] != 'tg':
                    raise Rejected('Нельзя удалить чужое сообщение MAX.')
                await mutate(
                    entry.client,
                    'delete_message',
                    chat_id=dialog['max_chat_id'],
                    message_ids=[int(link['max_message_id'])],
                    for_me=False,
                )
            return {'deleted': True}

        key = (
            link['tg_message_id']
            if job['direction'] == 'tg'
            else link['max_message_id']
        )
        await self.step(job, f'delete:{key}', remove)
        await self.db.remove_link(dialog, link['id'])

    async def change(self, entry, dialog, job, payload):
        if job['action'] in ('reaction', 'react', 'vote', 'poll_refresh'):
            return await self.interactions.deliver(entry, dialog, job, payload)

        origin = 'max' if job['direction'] == 'tg' else 'tg'
        old = await self.db.saved_step(job, 'edit-plan')
        if old is None:
            old = old_snapshot(
                await self.db.source_links(dialog, origin, job['source_id'])
            )
            await self.db.save_step(job, 'edit-plan', old)
        if not old:
            return
        if job['action'] == 'delete':
            for link in old:
                await self.delete_link(entry, dialog, job, link)
            return

        parts = payload.get('parts')
        if not isinstance(parts, list) or len(parts) != 1:
            raise Rejected('Некорректное задание редактирования.')
        normalized = canonical(parts[0])

        if origin == 'tg':
            return await self.send_max(
                entry,
                dialog,
                job,
                normalized,
                await self.reply(dialog, job, normalized),
                old,
            )

        units = tg_units(normalized)
        thread = await self.bridge.topic(dialog)
        regroup = any(row.get('album_id') for row in old) and (
            len(units) != len(old)
            or any(
                category(unit['kind']) != category(row['kind'])
                for unit, row in zip(units, old)
            )
        )
        if regroup:
            keep = set()
            for start, group in operations(units):
                result = await self.step(
                    job,
                    f'regroup:{start}',
                    lambda group=group: self.send_tg(
                        entry, dialog, job, group, thread, None
                    ),
                )
                await self.remember_tg(job, start, result)
                keep.update(item['id'] for item in result)
            for link in old:
                if link['tg_message_id'] not in keep:
                    await self.delete_link(entry, dialog, job, link)
            return

        for index, unit in enumerate(units):
            link = old[index] if index < len(old) else None
            compatible = link and (
                unit['kind'] == link['kind'] == 'text'
                or unit['kind'] in MEDIA and link['kind'] in MEDIA | {'text'}
                or unit['kind'] == link['kind'] == 'poll'
            )
            if compatible:
                async def edit(unit=unit, link=link):
                    params = {
                        'chat_id': dialog['owner'],
                        'message_id': link['tg_message_id'],
                    }
                    if unit['kind'] == 'poll':
                        return [await self.interactions.publish_poll(
                            dialog,
                            job['source_id'],
                            unit['attachment']['poll'],
                            thread,
                            None,
                        )]
                    await self.tg.pace(dialog['owner'])
                    try:
                        if unit['kind'] == 'text':
                            await self.tg.call(
                                'editMessageText',
                                {
                                    **params,
                                    'text': unit['text'],
                                    'entities': unit['entities'],
                                    'link_preview_options': {'is_disabled': True},
                                },
                            )
                            kind = 'text'
                        elif (
                            link.get('media_tag')
                            and tag(unit['attachment']) == link['media_tag']
                        ):
                            await self.tg.call(
                                'editMessageCaption',
                                {
                                    **params,
                                    'caption': unit['text'],
                                    'caption_entities': unit['entities'],
                                },
                            )
                            kind = link['kind']
                        else:
                            kind, name, raw = await self.upload_tg(
                                entry, dialog, job, unit
                            )
                            await self.tg.call(
                                'editMessageMedia',
                                {
                                    **params,
                                    'media': {
                                        'type': kind,
                                        'media': 'attach://file',
                                        'caption': unit['text'],
                                        'caption_entities': unit['entities'],
                                    },
                                },
                                files={'file': (name, raw)},
                            )
                    except TelegramRejected as exc:
                        if 'message is not modified' not in exc.description.lower():
                            raise
                        kind = unit['kind']
                    return [{
                        'id': link['tg_message_id'],
                        'kind': kind,
                        'album': link.get('album_id'),
                        'media_tag': (
                            tag(unit['attachment'])
                            if unit.get('attachment') else None
                        ),
                    }]

                result = await self.step(job, f'edit:{index}', edit)
            else:
                result = await self.step(
                    job,
                    f'replace:{index}',
                    lambda unit=unit: self.send_tg(
                        entry, dialog, job, [unit], thread, None
                    ),
                )
                if link:
                    await self.delete_link(entry, dialog, job, link)
            await self.remember_tg(job, index, result)

        for link in old[len(units):]:
            await self.delete_link(entry, dialog, job, link)
